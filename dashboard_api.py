
import os, json, time, hmac, hashlib, base64, secrets
from urllib.parse import urlencode
import aiohttp
from aiohttp import web
import discord

class DashboardAPI:
    def __init__(self, bot, state, guild_config, save_state, review_view):
        self.bot, self.state, self.guild_config, self.save_state, self.review_view = bot, state, guild_config, save_state, review_view
        self.cid=os.getenv("DISCORD_CLIENT_ID",""); self.secret=os.getenv("DISCORD_CLIENT_SECRET","")
        self.key=os.getenv("DASHBOARD_SESSION_SECRET",""); self.api=os.getenv("DASHBOARD_API_URL","").rstrip("/")
        self.front=os.getenv("DASHBOARD_URL","").rstrip("/"); self.port=int(os.getenv("DASHBOARD_PORT",os.getenv("PORT","8080")))
    def sign(self, data):
        raw=base64.urlsafe_b64encode(json.dumps(data,separators=(",",":")).encode()).decode().rstrip("=")
        return raw+"."+hmac.new(self.key.encode(),raw.encode(),hashlib.sha256).hexdigest()
    def verify(self, token):
        try:
            raw,sig=token.rsplit(".",1)
            if not hmac.compare_digest(sig,hmac.new(self.key.encode(),raw.encode(),hashlib.sha256).hexdigest()): return None
            data=json.loads(base64.urlsafe_b64decode(raw+"="*(-len(raw)%4)))
            return data if data.get("exp",0)>time.time() else None
        except Exception: return None
    @web.middleware
    async def cors(self, request, handler):
        if request.method=="OPTIONS":
            response=web.Response(status=204)
        else:
            if request.method in {"POST","PUT","PATCH","DELETE"} and request.headers.get("Origin") != self.front:
                raise web.HTTPForbidden(text="Cross-origin request blocked.")
            response=await handler(request)
        if request.headers.get("Origin")==self.front:
            response.headers.update({"Access-Control-Allow-Origin":self.front,"Access-Control-Allow-Credentials":"true","Access-Control-Allow-Headers":"Content-Type","Access-Control-Allow-Methods":"GET,POST,PUT,OPTIONS","Vary":"Origin"})
        return response
    def access(self, request, gid):
        s=self.verify(request.cookies.get("tsbaf_session"))
        if not s or str(gid) not in s.get("guilds",[]): raise web.HTTPForbidden(text="Manage Server permission is required.")
        g=self.bot.get_guild(int(gid))
        if not g: raise web.HTTPNotFound(text="Bot is not in this server.")
        return s,g
    async def login(self, request):
        if not all((self.cid,self.secret,self.key,self.api,self.front)): raise web.HTTPServiceUnavailable(text="OAuth environment variables are missing.")
        state=secrets.token_urlsafe(24); cookie=self.sign({"state":state,"exp":time.time()+600})
        uri=self.api+"/auth/callback"
        url="https://discord.com/oauth2/authorize?"+urlencode({"client_id":self.cid,"redirect_uri":uri,"response_type":"code","scope":"identify guilds","state":state})
        res=web.HTTPFound(url); res.set_cookie("tsbaf_oauth_state",cookie,max_age=600,httponly=True,secure=True,samesite="None"); raise res
    async def callback(self, request):
        st=self.verify(request.cookies.get("tsbaf_oauth_state"))
        if not st or st.get("state")!=request.query.get("state"): raise web.HTTPUnauthorized(text="OAuth state invalid.")
        async with aiohttp.ClientSession() as s:
            async with s.post("https://discord.com/api/oauth2/token",data={"client_id":self.cid,"client_secret":self.secret,"grant_type":"authorization_code","code":request.query.get("code"),"redirect_uri":self.api+"/auth/callback"}) as r:
                token=await r.json()
                if r.status!=200: raise web.HTTPUnauthorized(text="Discord token exchange failed.")
            headers={"Authorization":"Bearer "+token["access_token"]}
            async with s.get("https://discord.com/api/users/@me",headers=headers) as r: user=await r.json()
            async with s.get("https://discord.com/api/users/@me/guilds",headers=headers) as r: guilds=await r.json()
        allowed=[]
        for g in guilds if isinstance(guilds,list) else []:
            p=int(g.get("permissions","0"))
            if (p&8 or p&32) and self.bot.get_guild(int(g["id"])): allowed.append(str(g["id"]))
        cookie=self.sign({"user":str(user["id"]),"username":user.get("username",""),"guilds":allowed,"exp":time.time()+604800})
        res=web.HTTPFound(self.front+"/?connected=1"); res.set_cookie("tsbaf_session",cookie,max_age=604800,httponly=True,secure=True,samesite="None"); res.del_cookie("tsbaf_oauth_state"); raise res
    async def me(self, request):
        s=self.verify(request.cookies.get("tsbaf_session"))
        if not s: raise web.HTTPUnauthorized(text="Not connected.")
        gs=[]
        for gid in s.get("guilds",[]):
            g=self.bot.get_guild(int(gid))
            if g: gs.append({"id":gid,"name":g.name,"icon":str(g.icon.url) if g.icon else None})
        return web.json_response({"user":s.get("username"),"guilds":gs})
    async def logout(self, request):
        r=web.json_response({"ok":True}); r.del_cookie("tsbaf_session"); return r
    def resolve(self,guild,value,kind):
        if not value: return None
        raw=str(value).strip()
        if kind=="channel":
            raw=raw[2:-1] if raw.startswith("<#") and raw.endswith(">") else raw
            found=guild.get_channel(int(raw)) if raw.isdigit() else discord.utils.find(lambda c:isinstance(c,discord.TextChannel) and c.name.casefold()==raw.lstrip("#").casefold(),guild.text_channels)
        else:
            raw=raw[3:-1] if raw.startswith("<@&") and raw.endswith(">") else raw
            found=guild.get_role(int(raw)) if raw.isdigit() else discord.utils.find(lambda x:x.name.casefold()==raw.lstrip("@").casefold(),guild.roles)
        if not found: raise web.HTTPBadRequest(text=f"Unknown {kind}: {value}")
        return str(found.id)
    def display(self,guild):
        c=self.guild_config(guild.id); out=dict(c); out["guildId"]=str(guild.id); out["guildName"]=guild.name
        for k in ("logChannelId","blacklistLogChannelId","applicationChannelId","clanRegistrationChannelId","clanMainLogChannelId","lfClanChannelId"):
            ch=guild.get_channel(int(c[k])) if c.get(k) and str(c[k]).isdigit() else None; out[k]=ch.name if ch else ""
        for k in ("staffRoleId","blacklistPermsRoleId","blacklistRoleId","tryoutHostRoleId","clanLeaderRoleId","clanMainerRoleId","clanBlacklistRoleId","jailRoleId"):
            role=guild.get_role(int(c[k])) if c.get(k) and str(c[k]).isdigit() else None; out[k]=role.name if role else ""
        out["channels"]=[{"id":str(x.id),"name":x.name} for x in guild.text_channels]
        out["roles"]=[{"id":str(x.id),"name":x.name} for x in guild.roles if not x.is_default() and not x.managed]
        return out
    async def get_config(self,request):
        _,g=self.access(request,request.match_info["gid"]); return web.json_response(self.display(g))
    async def put_config(self,request):
        _,g=self.access(request,request.match_info["gid"]); body=await request.json(); c=self.guild_config(g.id)
        chkeys={"logChannelId","blacklistLogChannelId","applicationChannelId","clanRegistrationChannelId","clanMainLogChannelId","lfClanChannelId"}
        rkeys={"staffRoleId","blacklistPermsRoleId","blacklistRoleId","tryoutHostRoleId","clanLeaderRoleId","clanMainerRoleId","clanBlacklistRoleId","jailRoleId"}
        allowed=chkeys|rkeys|{"prefix","applicationQuestions","dashboardApplications"}
        if set(body)-allowed: raise web.HTTPBadRequest(text="Unsupported settings.")
        for k in chkeys&body.keys(): c[k]=self.resolve(g,body[k],"channel")
        for k in rkeys&body.keys(): c[k]=self.resolve(g,body[k],"role")
        if "prefix" in body:
            p=str(body["prefix"])
            if not 1<=len(p)<=5 or any(x.isspace() for x in p): raise web.HTTPBadRequest(text="Prefix must be 1–5 non-space characters.")
            c["prefix"]=p
        if "applicationQuestions" in body:
            q=body["applicationQuestions"]
            if not isinstance(q,list) or len(q)>5 or any(not isinstance(x,str) or not x.strip() for x in q): raise web.HTTPBadRequest(text="Use up to five application questions.")
            c["applicationQuestions"]=q
        if "dashboardApplications" in body:
            panels=body["dashboardApplications"]
            if not isinstance(panels,list) or len(panels)>25: raise web.HTTPBadRequest(text="Maximum 25 panels.")
            clean=[]
            incoming_ids={str(p.get("id","")) for p in panels if isinstance(p,dict)}
            for old_panel in cfg.get("dashboardApplications", []):
                if str(old_panel.get("id","")) not in incoming_ids and old_panel.get("messageId"):
                    old_channel_id=old_panel.get("panelChannel")
                    old_channel=g.get_channel(int(old_channel_id)) if str(old_channel_id).isdigit() else None
                    if isinstance(old_channel,discord.TextChannel):
                        try:
                            old_message=await old_channel.fetch_message(int(old_panel["messageId"]))
                            await old_message.edit(view=None)
                        except (discord.NotFound,discord.Forbidden,discord.HTTPException,ValueError):
                            pass
            for p in panels:
                qs=p.get("questions",[])
                if not isinstance(qs,list) or not 1<=len(qs)<=5 or any(not str(q.get("label","")).strip() or len(str(q.get("label","")))>45 for q in qs): raise web.HTTPBadRequest(text="Each panel needs 1–5 questions; labels must be 45 characters or less.")
                item={k:p.get(k,d) for k,d in {"id":"","name":"Application","description":"","panelTitle":"Application","panelDescription":"","buttonText":"Apply","color":"#d6a64b","panelChannel":"","reviewChannel":"","staffRole":"","enabled":False,"messageId":""}.items()}
                item["panelChannel"]=self.resolve(g,item["panelChannel"],"channel") if item["panelChannel"] else ""
                item["reviewChannel"]=self.resolve(g,item["reviewChannel"],"channel") if item["reviewChannel"] else ""
                item["staffRole"]=self.resolve(g,item["staffRole"],"role") if item["staffRole"] else ""
                item["questions"]=[{"label":str(q["label"]), "required":bool(q.get("required",True))} for q in qs]
                clean.append(item)
            c["dashboardApplications"]=clean
            for panel in clean:
                if not panel.get("enabled") and panel.get("messageId"):
                    channel_id=self.resolve(g,panel.get("panelChannel"),"channel") if panel.get("panelChannel") else None
                    channel=g.get_channel(int(channel_id)) if channel_id else None
                    if isinstance(channel,discord.TextChannel):
                        try:
                            old_message=await channel.fetch_message(int(panel["messageId"]))
                            await old_message.edit(view=None)
                        except (discord.NotFound,discord.Forbidden,discord.HTTPException,ValueError):
                            pass
                    panel["messageId"]=""
        self.save_state(); return web.json_response({"ok":True,"config":self.display(g)})
    async def publish(self,request):
        _,g=self.access(request,request.match_info["gid"]); c=self.guild_config(g.id)
        p=next((x for x in c.get("dashboardApplications",[]) if x.get("id")==request.match_info["pid"]),None)
        if not p: raise web.HTTPNotFound(text="Panel not found.")
        ch=self.resolve(g,p.get("panelChannel"),"channel"); channel=g.get_channel(int(ch)) if ch else None
        if not isinstance(channel,discord.TextChannel): raise web.HTTPBadRequest(text="Set a valid panel channel.")
        if not p.get("enabled"): raise web.HTTPBadRequest(text="Enable the panel first.")
        embed=discord.Embed(title=p["panelTitle"] or p["name"],description=p["panelDescription"] or p["description"],color=discord.Color.gold())
        view=ApplicationPanelView(self.bot,self.state,self.guild_config,self.save_state,self.review_view,g.id,p)
        msg=None
        if p.get("messageId"):
            try: msg=await channel.fetch_message(int(p["messageId"]))
            except (discord.NotFound,discord.Forbidden,discord.HTTPException,ValueError): pass
        if msg: await msg.edit(embed=embed,view=view)
        else: msg=await channel.send(embed=embed,view=view)
        p["messageId"]=str(msg.id); self.save_state()
        return web.json_response({"ok":True,"messageId":str(msg.id),"channel":channel.name})
    def app(self):
        if not all((self.cid,self.secret,self.key,self.api,self.front)):
            print("Dashboard API disabled: configure Discord OAuth and dashboard environment variables."); return None
        app=web.Application(middlewares=[self.cors],client_max_size=1024*1024)
        app.router.add_get("/auth/login",self.login); app.router.add_get("/auth/callback",self.callback)
        app.router.add_get("/api/me",self.me); app.router.add_post("/api/logout",self.logout)
        app.router.add_get("/api/guilds/{gid}/config",self.get_config); app.router.add_put("/api/guilds/{gid}/config",self.put_config)
        app.router.add_post("/api/guilds/{gid}/applications/{pid}/publish",self.publish)
        return app

class ApplicationPanelView(discord.ui.View):
    def __init__(self,bot,state,guild_config,save_state,review_view,guild_id,panel):
        super().__init__(timeout=None); self.bot=bot; self.state=state; self.guild_config=guild_config; self.save_state=save_state; self.review_view=review_view; self.guild_id=guild_id; self.panel=panel
        b=discord.ui.Button(label=panel.get("buttonText","Apply")[:80],style=discord.ButtonStyle.primary,custom_id=f"tsbaf:apply:{guild_id}:{panel['id']}")
        b.callback=self.open_modal; self.add_item(b)
    async def open_modal(self,interaction):
        if interaction.guild_id!=self.guild_id: return await interaction.response.send_message("Wrong server.",ephemeral=True)
        await interaction.response.send_modal(ApplicationPanelModal(self.bot,self.state,self.guild_config,self.save_state,self.review_view,self.guild_id,self.panel))

class ApplicationPanelModal(discord.ui.Modal):
    def __init__(self,bot,state,guild_config,save_state,review_view,guild_id,panel):
        super().__init__(title=str(panel.get("name","Application"))[:45],timeout=300)
        self.bot=bot; self.state=state; self.guild_config=guild_config; self.save_state=save_state; self.review_view=review_view; self.guild_id=guild_id; self.panel=panel; self.inputs=[]
        for i,q in enumerate(panel.get("questions",[])[:5]):
            x=discord.ui.TextInput(label=str(q.get("label",f"Question {i+1}"))[:45],required=bool(q.get("required",True)),style=discord.TextStyle.paragraph,max_length=1000)
            self.inputs.append(x); self.add_item(x)
    async def on_submit(self,interaction):
        guild=self.bot.get_guild(self.guild_id); cfg=self.guild_config(self.guild_id)
        raw=self.panel.get("reviewChannel") or cfg.get("applicationChannelId")
        channel=guild.get_channel(int(raw)) if guild and str(raw).isdigit() else None
        if not isinstance(channel,discord.TextChannel): return await interaction.response.send_message("Application review channel is not configured.",ephemeral=True)
        appid=f"{guild.id}-{interaction.user.id}-{int(time.time())}-{secrets.token_hex(3)}"
        answers=[str(x.value) for x in self.inputs]
        self.state["applications"][appid]={"id":appid,"guildId":str(guild.id),"userId":str(interaction.user.id),"answers":answers,"questions":[q.get("label","") for q in self.panel.get("questions",[])[:5]],"panelId":self.panel["id"],"panelName":self.panel.get("name","Application"),"reviewRoleId":str(self.panel.get("staffRole","")),"status":"pending","createdAt":discord.utils.utcnow().isoformat()}
        self.save_state()
        embed=discord.Embed(title=f"New {self.panel.get('name','Application')}",description=f"Applicant: {interaction.user.mention}\\nApplication ID: {appid}",color=discord.Color.gold())
        for q,a in zip(self.panel.get("questions",[])[:5],answers): embed.add_field(name=str(q.get("label","Answer"))[:256],value=a[:1024] or "No answer",inline=False)
        await channel.send(embed=embed,view=self.review_view("app",appid))
        await interaction.response.send_message("Application submitted for staff review.",ephemeral=True)

async def start_dashboard_api(bot,state,guild_config,save_state,review_view):
    api=DashboardAPI(bot,state,guild_config,save_state,review_view); app=api.app()
    if app is None: return
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",api.port).start()
    print(f"Dashboard API listening on port {api.port}")
