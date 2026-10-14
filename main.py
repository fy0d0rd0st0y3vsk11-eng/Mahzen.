import os, asyncio, secrets, json
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
import aiosqlite, discord, httpx
from discord.ext import commands, tasks
from discord import app_commands
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from dotenv import load_dotenv

load_dotenv()
TOKEN=os.getenv("DISCORD_TOKEN","")
OWNER=int(os.getenv("OWNER_DISCORD_ID","0") or 0)
AI_KEY=os.getenv("OPENROUTER_API_KEY","")
AI_MODEL=os.getenv("OPENROUTER_MODEL","openai/gpt-4o-mini")
DASH_PASS=os.getenv("DASHBOARD_PASSWORD","change-this-password")
DB=os.getenv("DATABASE_PATH","./mahzen.db")
TZ=os.getenv("TIMEZONE","Europe/Istanbul")
DEFAULT_PERSONALITY=("You are Fedya, the Mahzen gaming server's AI member. Speak naturally, casually and clearly in the user's language. Avoid forced memes, excessive emojis, and cringe roleplay. Give practical, accurate game instructions. Never invent links or lobby codes; use saved game info. If unsure, say so.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS players(user_id INTEGER PRIMARY KEY,username TEXT NOT NULL,total_points INTEGER NOT NULL DEFAULT 0,last_seen TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,game TEXT NOT NULL,starts_at TEXT NOT NULL,channel_id INTEGER NOT NULL,announcement TEXT DEFAULT '',mention_everyone INTEGER DEFAULT 0,role_id INTEGER,reminders TEXT DEFAULT '1440,360,120',status TEXT DEFAULT 'scheduled',deleted INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS rsvps(event_id INTEGER NOT NULL,user_id INTEGER NOT NULL,status TEXT NOT NULL,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(event_id,user_id));
CREATE TABLE IF NOT EXISTS attendance(event_id INTEGER NOT NULL,user_id INTEGER NOT NULL,confirmed INTEGER DEFAULT 0,points_awarded INTEGER DEFAULT 0,PRIMARY KEY(event_id,user_id));
CREATE TABLE IF NOT EXISTS games(game_key TEXT PRIMARY KEY,name TEXT NOT NULL,realms_url TEXT DEFAULT '',gartic_url TEXT DEFAULT '',amongus_code TEXT DEFAULT '',general_link TEXT DEFAULT '',instructions TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS achievements(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,description TEXT DEFAULT '',min_events INTEGER DEFAULT 0,min_games INTEGER DEFAULT 0,min_points INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS player_achievements(user_id INTEGER NOT NULL,achievement_id INTEGER NOT NULL,PRIMARY KEY(user_id,achievement_id));
CREATE TABLE IF NOT EXISTS intros(user_id INTEGER PRIMARY KEY,name TEXT NOT NULL,text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT,details TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY AUTOINCREMENT,component TEXT,message TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reminders_sent(event_id INTEGER,minutes_before INTEGER,PRIMARY KEY(event_id,minutes_before));
"""
async def dbconn():
    db=await aiosqlite.connect(DB); db.row_factory=aiosqlite.Row; return db
async def init_db():
    async with await dbconn() as db:
        await db.executescript(SCHEMA); await db.commit()
async def log(action,details=""):
    async with await dbconn() as db:
        await db.execute("INSERT INTO admin_log(action,details) VALUES(?,?)",(action,details[:1000])); await db.commit()
async def error(component,msg):
    async with await dbconn() as db:
        await db.execute("INSERT INTO errors(component,message) VALUES(?,?)",(component,str(msg)[:1500])); await db.commit()
async def setting(key,default=""):
    async with await dbconn() as db:
        r=await (await db.execute("SELECT value FROM settings WHERE key=?",(key,))).fetchone()
        return r["value"] if r else default
async def ensure_player(uid,name):
    async with await dbconn() as db:
        await db.execute("INSERT INTO players(user_id,username) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,last_seen=CURRENT_TIMESTAMP",(uid,name)); await db.commit()
async def game_info(text):
    t=text.casefold()
    async with await dbconn() as db: rows=await (await db.execute("SELECT * FROM games")).fetchall()
    for g in rows:
        if g["game_key"].casefold() in t or g["name"].casefold() in t or (g["game_key"]=="minecraft" and "realms" in t):
            if any(w in t for w in ("kod","code","lobi")): val,label=g["amongus_code"],"Kod"
            elif "realms" in t: val,label=g["realms_url"],"Realms bağlantısı"
            elif any(w in t for w in ("link","bağlantı","baglanti","url")): val,label=(g["general_link"] or g["gartic_url"] or g["realms_url"]),"Bağlantı"
            else: val,label=(g["instructions"] or g["general_link"] or g["realms_url"] or g["amongus_code"]),"Oyun bilgisi"
            if val: return f"**{g['name']} — {label}:** {val}"
    return None
async def ai_reply(prompt):
    if not AI_KEY: return "AI henüz yapılandırılmamış; yönetici OpenRouter API anahtarını eklemeli."
    personality=await setting("personality",DEFAULT_PERSONALITY)
    async with await dbconn() as db: games=await (await db.execute("SELECT * FROM games")).fetchall()
    context="\n".join([f"{g['name']}: Realms={g['realms_url']}; Gartic={g['gartic_url']}; Among Us code={g['amongus_code']}; Link={g['general_link']}; instructions={g['instructions']}" for g in games])
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r=await c.post("https://openrouter.ai/api/v1/chat/completions",headers={"Authorization":f"Bearer {AI_KEY}","Content-Type":"application/json","X-Title":"Mahzen Fedya"},json={"model":AI_MODEL,"messages":[{"role":"system","content":personality+"\nUse this trusted game information and do not invent missing details:\n"+context},{"role":"user","content":prompt[:3000]}],"temperature":0.7,"max_tokens":700})
            r.raise_for_status(); return r.json()["choices"][0]["message"]["content"][:1900]
    except Exception as e:
        await error("openrouter",repr(e)); return "AI yanıt verirken bağlantı hatası oluştu. Hata dashboard'a kaydedildi."

intents=discord.Intents.default(); intents.members=True; intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)
class RSVPView(discord.ui.View):
    def __init__(self,eid):
        super().__init__(timeout=None); self.eid=eid
    async def save(self,i,status):
        await ensure_player(i.user.id,i.user.display_name)
        async with await dbconn() as db:
            ev=await (await db.execute("SELECT id FROM events WHERE id=? AND status='scheduled' AND deleted=0",(self.eid,))).fetchone()
            if not ev: await i.response.send_message("Bu etkinlik artık açık değil.",ephemeral=True); return
            await db.execute("INSERT INTO rsvps(event_id,user_id,status) VALUES(?,?,?) ON CONFLICT(event_id,user_id) DO UPDATE SET status=excluded.status,updated_at=CURRENT_TIMESTAMP",(self.eid,i.user.id,status))
            rs=await (await db.execute("SELECT status,COUNT(*) n FROM rsvps WHERE event_id=? GROUP BY status",(self.eid,))).fetchall(); await db.commit()
        counts={r["status"]:r["n"] for r in rs}
        await i.response.send_message(f"Yanıtın kaydedildi. Katılıyor: {counts.get('yes',0)} · Katılamıyor: {counts.get('no',0)}",ephemeral=True)
        try:
            if i.message and i.message.embeds:
                em=i.message.embeds[0]; em.set_footer(text=f"Katılıyor: {counts.get('yes',0)} · Katılamıyor: {counts.get('no',0)}")
                await i.message.edit(embed=em,view=self)
        except Exception: pass
    @discord.ui.button(label="Katılıyorum",style=discord.ButtonStyle.success,emoji="✅",custom_id="rsvp_yes")
    async def yes(self,i,b): await self.save(i,"yes")
    @discord.ui.button(label="Katılamıyorum",style=discord.ButtonStyle.secondary,emoji="❌",custom_id="rsvp_no")
    async def no(self,i,b): await self.save(i,"no")

async def announce(ev):
    try: ch=bot.get_channel(int(ev["channel_id"])) or await bot.fetch_channel(int(ev["channel_id"]))
    except Exception as e: await error("announcement",e); return
    mention="@everyone " if ev["mention_everyone"] else ""
    if ev["role_id"]: mention+=f"<@&{ev['role_id']}> "
    em=discord.Embed(title="🎮 "+ev["title"],description=ev["announcement"] or "Oyun zamanı yaklaşıyor!",color=0x2E8B57)
    em.add_field(name="Oyun",value=ev["game"],inline=True); em.add_field(name="Saat",value=ev["starts_at"].replace("T"," "),inline=True)
    try:
        info=await game_info(ev["game"]+" link")
        if info: em.add_field(name="Oyun bilgisi",value=info[:1000],inline=False)
        await ch.send(content=mention or None,embed=em,view=RSVPView(ev["id"]),allowed_mentions=discord.AllowedMentions(everyone=bool(ev["mention_everyone"]),roles=True,users=False))
    except Exception as e: await error("announcement",e)

@tasks.loop(seconds=30)
async def reminder_loop():
    now=datetime.now(ZoneInfo(TZ))
    async with await dbconn() as db: events=await (await db.execute("SELECT * FROM events WHERE status='scheduled' AND deleted=0")).fetchall()
    for ev in events:
        try:
            start=datetime.fromisoformat(ev["starts_at"])
            if start.tzinfo is None: start=start.replace(tzinfo=ZoneInfo(TZ))
            left=int((start-now).total_seconds()//60)
            for m in [int(x) for x in ev["reminders"].split(",") if x.strip().isdigit()]:
                if m-1 < left <= m:
                    async with await dbconn() as db:
                        sent=await (await db.execute("SELECT 1 FROM reminders_sent WHERE event_id=? AND minutes_before=?",(ev["id"],m))).fetchone()
                        if sent: continue
                        await db.execute("INSERT OR IGNORE INTO reminders_sent VALUES(?,?)",(ev["id"],m)); await db.commit()
                    await announce(ev)
        except Exception as e: await error("scheduler",e)
@reminder_loop.before_loop
async def before_loop(): await bot.wait_until_ready()
@bot.event
async def setup_hook():
    pass
@bot.event
async def on_ready():
    print("Fedya ready:",bot.user)
    if not reminder_loop.is_running(): reminder_loop.start()
    async with await dbconn() as db: evs=await (await db.execute("SELECT id FROM events WHERE status='scheduled' AND deleted=0")).fetchall()
    for e in evs: bot.add_view(RSVPView(e["id"]))
@bot.event
async def on_member_join(member): await ensure_player(member.id,member.display_name)
@bot.event
async def on_message(message):
    if message.author.bot: return
    await ensure_player(message.author.id,message.author.display_name)
    t=message.content; low=t.casefold()
    if any(s in low for s in ["botu kim yaptı","botu kim yapti","botu kim tasarladı","botu kim tasarladi","botun sahibi kim","kim geliştirdi","kim gelistirdi"]):
        await message.reply(f"Bu botu geliştiren: <@{OWNER}>" if OWNER else "Geliştirici hesabı ayarlanmamış.",mention_author=False); return
    info=await game_info(t)
    if info and any(s in low for s in ["link","bağlantı","baglanti","realms","kod","code","lobi","url"]):
        await message.reply(info,mention_author=False); return
    if bot.user and bot.user in message.mentions:
        prompt=t.replace(f"<@{bot.user.id}>","").replace(f"<@!{bot.user.id}>","").strip()
        await message.reply(await ai_reply(prompt or "Kendini kısaca tanıt."),mention_author=False)
    await bot.process_commands(message)

@bot.tree.command(name="profil",description="Oyuncu profilini gösterir.")
@app_commands.describe(uye="İsteğe bağlı başka üye")
async def profil(i:discord.Interaction,uye:discord.Member|None=None):
    u=uye or i.user
    async with await dbconn() as db:
        p=await (await db.execute("SELECT * FROM players WHERE user_id=?",(u.id,))).fetchone()
        total=await (await db.execute("SELECT COUNT(*) n FROM attendance WHERE user_id=? AND confirmed=1",(u.id,))).fetchone()
        games=await (await db.execute("SELECT e.game,COUNT(*) n FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.user_id=? AND a.confirmed=1 GROUP BY e.game ORDER BY n DESC",(u.id,))).fetchall()
        last=await (await db.execute("SELECT e.title FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.user_id=? AND a.confirmed=1 ORDER BY e.starts_at DESC LIMIT 1",(u.id,))).fetchone()
        ach=await (await db.execute("SELECT a.name FROM player_achievements pa JOIN achievements a ON a.id=pa.achievement_id WHERE pa.user_id=?",(u.id,))).fetchall()
    if not p: await i.response.send_message("Profil henüz oluşmamış.",ephemeral=True); return
    em=discord.Embed(title=f"🎮 {u.display_name} — Oyuncu Profili",color=0x2E8B57)
    em.add_field(name="Puan",value=p["total_points"]); em.add_field(name="Onaylı katılım",value=total["n"])
    em.add_field(name="En çok oynadığı oyun",value=games[0]["game"] if games else "Henüz yok",inline=False)
    em.add_field(name="Son etkinlik",value=last["title"] if last else "Henüz yok",inline=False)
    em.add_field(name="Başarımlar",value=", ".join(a["name"] for a in ach) or "Henüz yok",inline=False)
    await i.response.send_message(embed=em)
@bot.tree.command(name="puanlar",description="En yüksek puanlı 10 oyuncu.")
async def puanlar(i:discord.Interaction):
    async with await dbconn() as db: rows=await (await db.execute("SELECT user_id,total_points FROM players ORDER BY total_points DESC LIMIT 10")).fetchall()
    await i.response.send_message("\n".join(f"{n}. <@{r['user_id']}> — {r['total_points']} puan" for n,r in enumerate(rows,1)) or "Henüz puan yok.")
@bot.tree.command(name="oyunbilgi",description="Kayıtlı oyun bilgilerini gösterir.")
async def oyunbilgi(i:discord.Interaction,oyun:str):
    info=await game_info(oyun+" link")
    await i.response.send_message(info or "Bu oyun için bilgi kaydedilmemiş.",ephemeral=not bool(info))
@bot.tree.command(name="etkinlikler",description="Yaklaşan etkinlikleri gösterir.")
async def etkinlikler(i:discord.Interaction):
    async with await dbconn() as db: rows=await (await db.execute("SELECT title,game,starts_at FROM events WHERE status='scheduled' AND deleted=0 ORDER BY starts_at LIMIT 10")).fetchall()
    await i.response.send_message("\n".join(f"• **{e['title']}** ({e['game']}) — {e['starts_at']}" for e in rows) or "Etkinlik yok.")
@bot.tree.command(name="tanit",description="Kayıtlı bir üyeyi tanıtır.")
async def tanit(i:discord.Interaction,uye:discord.Member):
    async with await dbconn() as db: row=await (await db.execute("SELECT name,text FROM intros WHERE user_id=?",(uye.id,))).fetchone()
    await i.response.send_message(f"**{row['name']}**\n{row['text']}" if row else "Bu kişi için tanıtım eklenmemiş.",ephemeral=not bool(row))
@bot.tree.command(name="yardim",description="Fedya komutlarını gösterir.")
async def yardim(i:discord.Interaction):
    await i.response.send_message("`/profil` · `/puanlar` · `/oyunbilgi oyun:<isim>` · `/etkinlikler` · `/tanit uye:<kişi>`")

# FastAPI dashboard: one admin password, session cookie, server-rendered HTML.
app=FastAPI(title="Mahzen Fedya")
@app.on_event("startup")
async def startup():
    await init_db()
    app.state.cookie=secrets.token_urlsafe(32)
    if TOKEN: asyncio.create_task(bot.start(TOKEN))
@app.on_event("shutdown")
async def shutdown():
    if bot.is_ready(): await bot.close()
def auth(req):
    if req.cookies.get("mahzen_auth") != app.state.cookie: raise HTTPException(303,headers={"Location":"/login"})
def page(title,body):
    nav='<nav><a href="/">Genel Bakış</a><a href="/events">Etkinlikler</a><a href="/players">Oyuncular</a><a href="/games">Oyun bilgileri</a><a href="/ai">AI ayarları</a><a href="/intros">Tanıtımlar</a><a href="/achievements">Başarımlar</a><a href="/system">Sistem</a></nav>'
    return HTMLResponse(f"""<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#0b100e;color:#e6eee8;font:15px system-ui}}header{{padding:18px 24px;background:#101813;border-bottom:1px solid #2b3b31;font-weight:900}}header span{{color:#79c994}}.layout{{display:grid;grid-template-columns:200px 1fr}}nav{{padding:20px;display:flex;flex-direction:column;gap:10px;border-right:1px solid #2b3b31}}a{{color:#79c994;text-decoration:none}}main{{padding:24px;max-width:1300px}}.card{{background:#131c17;border:1px solid #2b3b31;border-radius:12px;padding:18px;margin:16px 0}}input,textarea,select{{background:#0d1410;color:#eee;border:1px solid #35483b;border-radius:7px;padding:10px;width:100%;margin:4px 0 12px}}button{{background:#245e3a;color:white;border:0;border-radius:7px;padding:10px 14px;cursor:pointer}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #2b3b31;text-align:left;vertical-align:top}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}small,.muted{{color:#9eafa3}}.good{{color:#79c994}}.bad{{color:#e28c8c}}@media(max-width:700px){{.layout{{grid-template-columns:1fr}}nav{{display:grid;grid-template-columns:repeat(2,1fr);border-right:0;border-bottom:1px solid #2b3b31}}main{{padding:12px}}.grid{{grid-template-columns:1fr}}}}</style><header>🍄 MAHZEN <span>FEDYA DASHBOARD</span><form method="post" action="/logout" style="float:right"><button>Çıkış</button></form></header><div class="layout">{nav}<main><h1>{title}</h1>{body}</main></div></html>""")
@app.get("/login",response_class=HTMLResponse)
async def login_page(): return HTMLResponse('<body style="background:#0b100e;color:white;font:16px system-ui;max-width:420px;margin:12vh auto"><h1>Mahzen · Fedya</h1><form method="post"><input type="password" name="password" placeholder="Dashboard şifresi" required style="padding:12px;width:100%"><button style="padding:12px;margin-top:12px">Giriş yap</button></form></body>')
@app.post("/login")
async def login(password:str=Form(...)):
    if not secrets.compare_digest(password,DASH_PASS): return HTMLResponse("Şifre yanlış. <a href='/login'>Tekrar dene</a>",status_code=401)
    r=RedirectResponse("/",303); r.set_cookie("mahzen_auth",app.state.cookie,httponly=True,samesite="lax",max_age=43200); return r
@app.post("/logout")
async def logout():
    r=RedirectResponse("/login",303); r.delete_cookie("mahzen_auth"); return r
@app.get("/",response_class=HTMLResponse)
async def home(req:Request):
    auth(req)
    async with await dbconn() as db:
        n=(await (await db.execute("SELECT COUNT(*) n FROM players")).fetchone())["n"]
        ev=await (await db.execute("SELECT * FROM events WHERE deleted=0 ORDER BY starts_at LIMIT 10")).fetchall()
        errs=await (await db.execute("SELECT COUNT(*) n FROM errors")).fetchone()
    body=f'<div class="card">Discord: <b class="{"good" if bot.is_ready() else "bad"}">{"Çevrim içi" if bot.is_ready() else "Çevrim dışı"}</b> · AI: <b>{"Hazır" if AI_KEY else "Anahtar yok"}</b> · Oyuncular: {n} · Hatalar: {errs["n"]}</div><div class="card"><h2>Yaklaşan etkinlikler</h2><table><tr><th>Ad</th><th>Oyun</th><th>Saat</th><th>ID</th></tr>'+''.join(f"<tr><td>{e['title']}</td><td>{e['game']}</td><td>{e['starts_at']}</td><td>{e['id']}</td></tr>" for e in ev)+'</table></div>'
    return page("Genel Bakış",body)
@app.get("/events",response_class=HTMLResponse)
async def events_page(req:Request):
    auth(req)
    async with await dbconn() as db: ev=await (await db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")).fetchall()
    rows=''.join(f"<tr><td>{e['id']}</td><td>{e['title']}</td><td>{e['game']}</td><td>{e['starts_at']}</td><td>{e['status']}</td><td><form method='post' action='/events/{e['id']}/announce'><button>Şimdi duyur</button></form><form method='post' action='/events/{e['id']}/delete'><button>Sil</button></form></td></tr>" for e in ev)
    body="""<div class="card"><h2>Yeni etkinlik</h2><form method="post" action="/events/create" class="grid">
    <label>Etkinlik adı<input name="title" required></label><label>Oyun<input name="game" required placeholder="Minecraft / Among Us / Gartic Phone"></label>
    <label>Tarih ve saat<input type="datetime-local" name="starts_at" required></label><label>Discord kanal ID<input type="number" name="channel_id" required></label>
    <label>Etiketlenecek rol ID (opsiyonel)<input type="number" name="role_id"></label><label>Hatırlatmalar, dakika önce<input name="reminders" value="1440,360,120" required></label>
    <label>Duyuru metni<textarea name="announcement"></textarea></label><label><input type="checkbox" name="everyone" value="yes"> @everyone etiketle</label><button>Etkinlik oluştur</button></form></div>"""
    return page("Etkinlikler",body+f'<div class="card"><h2>Etkinlik geçmişi</h2><table><tr><th>ID</th><th>Ad</th><th>Oyun</th><th>Saat</th><th>Durum</th><th>İşlem</th></tr>{rows}</table></div>')
@app.post("/events/create")
async def event_create(req:Request,title:str=Form(...),game:str=Form(...),starts_at:str=Form(...),channel_id:int=Form(...),role_id:str=Form(""),reminders:str=Form("1440,360,120"),announcement:str=Form(""),everyone:str|None=Form(None)):
    auth(req)
    dt=datetime.fromisoformat(starts_at)
    if dt.tzinfo is None: dt=dt.replace(tzinfo=ZoneInfo(TZ))
    rem=",".join(x for x in reminders.split(",") if x.strip().isdigit())
    async with await dbconn() as db:
        c=await db.execute("INSERT INTO events(title,game,starts_at,channel_id,announcement,mention_everyone,role_id,reminders) VALUES(?,?,?,?,?,?,?,?)",(title,game,dt.isoformat(timespec="minutes"),channel_id,announcement,1 if everyone else 0,int(role_id) if role_id.isdigit() else None,rem)); await db.commit()
    await log("event_create",f"id={c.lastrowid} {title}"); return RedirectResponse("/events",303)
@app.post("/events/{eid}/announce")
async def event_announce(req:Request,eid:int):
    auth(req)
    async with await dbconn() as db: ev=await (await db.execute("SELECT * FROM events WHERE id=?",(eid,))).fetchone()
    if not ev: raise HTTPException(404)
    if not bot.is_ready(): raise HTTPException(503,"Bot çevrim içi değil")
    await announce(ev); await log("event_announce",str(eid)); return RedirectResponse("/events",303)
@app.post("/events/{eid}/delete")
async def event_delete(req:Request,eid:int):
    auth(req)
    async with await dbconn() as db: await db.execute("UPDATE events SET deleted=1,status='cancelled' WHERE id=?",(eid,)); await db.commit()
    await log("event_delete",str(eid)); return RedirectResponse("/events",303)
@app.get("/players",response_class=HTMLResponse)
async def players_page(req:Request,q:str=""):
    auth(req)
    async with await dbconn() as db:
        rows=await (await db.execute("SELECT * FROM players WHERE username LIKE ? OR CAST(user_id AS TEXT) LIKE ? ORDER BY total_points DESC LIMIT 100",(f"%{q}%",f"%{q}%"))).fetchall()
        evs=await (await db.execute("SELECT id,title FROM events WHERE deleted=0 ORDER BY id DESC LIMIT 100")).fetchall()
    body=f'<div class="card"><form><input name="q" placeholder="Oyuncu adı veya ID" value="{q}"><button>Ara</button></form><table><tr><th>Oyuncu</th><th>ID</th><th>Puan</th><th>Puan düzenle</th></tr>'
    body+=''.join(f"<tr><td>{p['username']}</td><td>{p['user_id']}</td><td>{p['total_points']}</td><td><form method='post' action='/players/{p['user_id']}/points'><input name='amount' type='number' value='1'><input name='reason' value='Yönetici düzeltmesi'><button>Uygula</button></form></td></tr>" for p in rows)+'</table></div>'
    body+='<div class="card"><h2>Gerçek katılımı onayla</h2><p>Katılım onaylanınca puan verilir. Etkinlik ID ve Discord kullanıcı ID kullan.</p><form method="post" action="/attendance"><div class="grid"><label>Etkinlik ID<input type="number" name="event_id" required></label><label>Kullanıcı ID<input type="number" name="user_id" required></label><label>Puan<input type="number" name="points" value="1" min="0"></label><label>Durum<select name="confirmed"><option value="yes">Katıldı</option><option value="no">Katılımı kaldır</option></select></label></div><button>Kaydet</button></form><small>'+ ' · '.join(f"#{e['id']} {e['title']}" for e in evs)+'</small></div>'
    return page("Oyuncular",body)
@app.post("/players/{uid}/points")
async def point_change(req:Request,uid:int,amount:int=Form(...),reason:str=Form("Düzeltme")):
    auth(req)
    async with await dbconn() as db:
        await db.execute("INSERT OR IGNORE INTO players(user_id,username) VALUES(?,?)",(uid,str(uid)))
        await db.execute("UPDATE players SET total_points=MAX(0,total_points+?) WHERE user_id=?",(amount,uid)); await db.commit()
    await log("points_adjust",f"{uid} {amount}: {reason}"); return RedirectResponse("/players",303)
@app.post("/attendance")
async def attendance(req:Request,event_id:int=Form(...),user_id:int=Form(...),points:int=Form(1),confirmed:str=Form("yes")):
    auth(req)
    async with await dbconn() as db:
        await db.execute("INSERT OR IGNORE INTO players(user_id,username) VALUES(?,?)",(user_id,str(user_id)))
        old=await (await db.execute("SELECT * FROM attendance WHERE event_id=? AND user_id=?",(event_id,user_id))).fetchone()
        if confirmed=="yes":
            if not old or not old["points_awarded"]: await db.execute("UPDATE players SET total_points=total_points+? WHERE user_id=?",(points,user_id))
            await db.execute("INSERT INTO attendance(event_id,user_id,confirmed,points_awarded) VALUES(?,?,1,?) ON CONFLICT(event_id,user_id) DO UPDATE SET confirmed=1,points_awarded=MAX(attendance.points_awarded,excluded.points_awarded)",(event_id,user_id,points))
        else:
            if old and old["points_awarded"]: await db.execute("UPDATE players SET total_points=MAX(0,total_points-?) WHERE user_id=?",(old["points_awarded"],user_id))
            await db.execute("UPDATE attendance SET confirmed=0,points_awarded=0 WHERE event_id=? AND user_id=?",(event_id,user_id))
        await db.commit()
    await log("attendance_confirm",f"{event_id} {user_id} {confirmed}"); return RedirectResponse("/players",303)
@app.get("/games",response_class=HTMLResponse)
async def games_page(req:Request):
    auth(req)
    async with await dbconn() as db: rows=await (await db.execute("SELECT * FROM games ORDER BY name")).fetchall()
    body="""<div class="card"><h2>Oyun kaydet / güncelle</h2><form method="post" action="/games/save" class="grid">
    <label>Anahtar<input name="key" required placeholder="minecraft"></label><label>Görünen ad<input name="name" required></label>
    <label>Realms URL<input name="realms"></label><label>Gartic URL<input name="gartic"></label><label>Among Us kodu<input name="code"></label><label>Genel link<input name="link"></label>
    <label>Katılma talimatları<textarea name="instructions"></textarea></label><button>Kaydet</button></form></div><div class="card"><h2>Kayıtlı oyunlar</h2><table><tr><th>Anahtar</th><th>Ad</th><th>Link/kod</th></tr>"""
    body+=''.join(f"<tr><td>{g['game_key']}</td><td>{g['name']}</td><td>{g['realms_url']} {g['gartic_url']} {g['amongus_code']} {g['general_link']}</td></tr>" for g in rows)+'</table></div>'
    return page("Oyun Bilgileri",body)
@app.post("/games/save")
async def games_save(req:Request,key:str=Form(...),name:str=Form(...),realms:str=Form(""),gartic:str=Form(""),code:str=Form(""),link:str=Form(""),instructions:str=Form("")):
    auth(req)
    async with await dbconn() as db:
        await db.execute("INSERT INTO games(game_key,name,realms_url,gartic_url,amongus_code,general_link,instructions) VALUES(?,?,?,?,?,?,?) ON CONFLICT(game_key) DO UPDATE SET name=excluded.name,realms_url=excluded.realms_url,gartic_url=excluded.gartic_url,amongus_code=excluded.amongus_code,general_link=excluded.general_link,instructions=excluded.instructions",(key.lower(),name,realms,gartic,code,link,instructions)); await db.commit()
    await log("game_info_save",key); return RedirectResponse("/games",303)
@app.get("/ai",response_class=HTMLResponse)
async def ai_page(req:Request):
    auth(req); personality=await setting("personality",DEFAULT_PERSONALITY)
    return page("AI Ayarları",f'<div class="card"><form method="post" action="/ai/save"><label>AI kişiliği<textarea name="personality" rows="12" required>{personality}</textarea></label><button>Kaydet</button></form><p class="muted">Model: {AI_MODEL} (OPENROUTER_MODEL ortam değişkeni)</p></div>')
@app.post("/ai/save")
async def ai_save(req:Request,personality:str=Form(...)):
    auth(req)
    async with await dbconn() as db: await db.execute("INSERT INTO settings(key,value) VALUES('personality',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(personality[:8000],)); await db.commit()
    await log("ai_settings_save","personality updated"); return RedirectResponse("/ai",303)
@app.get("/intros",response_class=HTMLResponse)
async def intros_page(req:Request):
    auth(req)
    async with await dbconn() as db: rows=await (await db.execute("SELECT * FROM intros ORDER BY name")).fetchall()
    body='<div class="card"><form method="post" action="/intros/save" class="grid"><label>Discord ID<input name="uid" type="number" required></label><label>Görünen ad<input name="name" required></label><label>Tanıtım metni<textarea name="text" required></textarea></label><button>Kaydet</button></form></div><div class="card">'
    body+=''.join(f"<p><b>{r['name']}</b> · {r['user_id']}<br>{r['text']}</p>" for r in rows)+'</div>'
    return page("Üye Tanıtımları",body)
@app.post("/intros/save")
async def intros_save(req:Request,uid:int=Form(...),name:str=Form(...),text:str=Form(...)):
    auth(req)
    async with await dbconn() as db: await db.execute("INSERT INTO intros(user_id,name,text) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,text=excluded.text",(uid,name,text)); await db.commit()
    await log("intro_save",str(uid)); return RedirectResponse("/intros",303)
@app.get("/achievements",response_class=HTMLResponse)
async def achievements_page(req:Request):
    auth(req)
    async with await dbconn() as db: rows=await (await db.execute("SELECT * FROM achievements ORDER BY id DESC")).fetchall()
    body='<div class="card"><form method="post" action="/achievements/save" class="grid"><label>Ad<input name="name" required></label><label>Açıklama<input name="description"></label><label>Min. etkinlik<input name="min_events" type="number" value="1" min="0"></label><label>Min. farklı oyun<input name="min_games" type="number" value="0" min="0"></label><label>Min. puan<input name="min_points" type="number" value="0" min="0"></label><button>Başarım ekle</button></form></div><div class="card"><table><tr><th>Ad</th><th>Açıklama</th><th>Etkinlik</th><th>Farklı oyun</th><th>Puan</th></tr>'
    body+=''.join(f"<tr><td>{r['name']}</td><td>{r['description']}</td><td>{r['min_events']}</td><td>{r['min_games']}</td><td>{r['min_points']}</td></tr>" for r in rows)+'</table></div>'
    return page("Başarımlar",body)
@app.post("/achievements/save")
async def achievement_save(req:Request,name:str=Form(...),description:str=Form(""),min_events:int=Form(0),min_games:int=Form(0),min_points:int=Form(0)):
    auth(req)
    async with await dbconn() as db: await db.execute("INSERT INTO achievements(name,description,min_events,min_games,min_points) VALUES(?,?,?,?,?)",(name,description,max(0,min_events),max(0,min_games),max(0,min_points))); await db.commit()
    await log("achievement_create",name); return RedirectResponse("/achievements",303)
@app.get("/system",response_class=HTMLResponse)
async def system_page(req:Request):
    auth(req)
    async with await dbconn() as db:
        errs=await (await db.execute("SELECT * FROM errors ORDER BY id DESC LIMIT 40")).fetchall()
        logs=await (await db.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT 40")).fetchall()
        reminders=await (await db.execute("SELECT * FROM reminders_sent ORDER BY rowid DESC LIMIT 30")).fetchall()
    body=f'<div class="card">Discord: <b class="{"good" if bot.is_ready() else "bad"}">{"Çevrim içi" if bot.is_ready() else "Çevrim dışı"}</b> · OpenRouter: {"yapılandırılmış" if AI_KEY else "anahtar yok"}</div><div class="card"><h2>Hatalar</h2>'+''.join(f"<p>{e['created_at']} · <b>{e['component']}</b> · {e['message']}</p>" for e in errs)+'</div><div class="card"><h2>İşlem günlüğü</h2>'+''.join(f"<p>{l['created_at']} · <b>{l['action']}</b> · {l['details']}</p>" for l in logs)+'</div><div class="card"><h2>Gönderilen hatırlatmalar</h2>'+''.join(f"<p>Etkinlik {r['event_id']} · {r['minutes_before']} dakika önce</p>" for r in reminders)+'</div>'
    return page("Sistem & Günlükler",body)
@app.get("/health")
async def health(): return {"status":"ok","discord_ready":bot.is_ready(),"ai_configured":bool(AI_KEY)}
if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host=os.getenv("HOST","0.0.0.0"),port=int(os.getenv("PORT","8000")))
