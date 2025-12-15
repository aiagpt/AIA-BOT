"""
extraction.py - Motor de extração adaptado para Multi-Server (Guild Sharding)
Atualizado: Arquivos agora são tratados como Links (sem download físico)
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import os
import shutil
import asyncio
import traceback
from datetime import datetime, time, timedelta

# Importa da nova configuração isolada
from config import (
    DataManager, get_config, get_categories, get_setup_id,
    clean_name, registrar_log_safe, log_resolution_safe, remove_resolution,
    update_config, get_all_active_guilds,
    BRT_OFFSET, HORA_BACKUP, MINUTO_BACKUP, execute_with_retry as executar_com_retry
)

# Importa as Views atualizadas
from ui_components import (
    PainelSetup, PainelPrincipal, PainelResolucao, ExtractionChannelSelectView,
    build_dashboard_embed
)

_bot_instance = None

def set_bot(bot):
    """Define referência global do bot para uso em callbacks"""
    global _bot_instance
    _bot_instance = bot

async def apagar_mensagens_antigas_bot(bot: commands.Bot, thread: discord.Thread, texto_para_buscar: str) -> None:
    """Remove mensagens antigas do bot para manter o tópico limpo"""
    try:
        async for msg in thread.history(limit=50):
            if msg.author.id != bot.user.id: continue
            should_delete = False
            if msg.content and texto_para_buscar in msg.content: should_delete = True
            if not should_delete and msg.embeds:
                for embed in msg.embeds:
                    if embed.title and texto_para_buscar in embed.title: should_delete = True; break
            if should_delete: await msg.delete(); await asyncio.sleep(0.5)
    except: pass

async def finalizar_topico_logica(interaction, selections, guild_id):
    """Callback final chamado pela UI de resolução"""
    thread = interaction.channel
    orgao = selections["orgao"]
    cat = selections["categoria"]
    quem = selections["quem_tratou"]
    
    # Limpa mensagens de status anteriores
    await apagar_mensagens_antigas_bot(_bot_instance, thread, "Tópico Reaberto!")
    await apagar_mensagens_antigas_bot(_bot_instance, thread, "Chamado Finalizado!")
    
    # Registra a resolução no banco de dados específico do servidor
    await log_resolution_safe(guild_id, thread.id, thread.name, quem, interaction.user.id, cat, orgao)
    
    embed = discord.Embed(title="✅ Chamado Finalizado!", color=0x2ecc71)
    embed.add_field(name="Orgão", value=orgao)
    embed.add_field(name="Categoria", value=cat)
    embed.add_field(name="Resolvido por", value=quem)
    embed.set_footer(text=f"Fechado por {interaction.user.display_name}")
    
    if interaction.response.is_done(): await interaction.edit_original_response(content=None, embed=embed, view=None)
    else: await interaction.response.edit_message(content=None, embed=embed, view=None)
    
    # --- LÓGICA DE RENOMEAÇÃO E FECHAMENTO OTIMIZADA ---
    try:
        # 1. Calcula o novo nome
        prefixes = ["OK - ", "OK ", "[OK] ", "[OK]", "(OK) ", "(OK)"]
        new_name = thread.name
        has_prefix = False
        
        for p in prefixes:
            if new_name.startswith(p):
                has_prefix = True
                break
                
        if not has_prefix:
            new_name = f"OK - {new_name}"
        
        # 2. Verifica se o nome REALMENTE mudou
        name_changed = (new_name != thread.name)

        # 3. Verifica se já está arquivado (Evita erro 50083)
        if thread.archived:
            await thread.edit(locked=True, archived=True)
            return

        # 4. Executa a Ação com Proteção de Timeout (5 segundos)
        if name_changed:
            try:
                # Tenta Renomear + Trancar + Arquivar
                # Se demorar mais que 5s (Rate Limit), o asyncio cancela e vai pro except
                await asyncio.wait_for(thread.edit(
                    name=new_name,
                    locked=True,
                    archived=True,
                    reason="Finalizado via Bot"
                ), timeout=5.0)
            except asyncio.TimeoutError:
                # Rate limit detectado! Desiste de renomear e só tranca.
                print(f"⚠️ Rate Limit no tópico {thread.name}. Aplicando fallback (sem renomear).")
                
                # --- AVISO NO CHAT (Plano B) ---
                try:
                    await thread.send("⚠️ **Aviso de Limite:** O Discord impediu a renomeação deste tópico (Rate Limit). Por favor, **adicione o 'OK' manualmente**.")
                except: pass
                # -------------------------------

                await thread.edit(
                    locked=True,
                    archived=True,
                    reason="Finalizado via Bot (Fallback Rate Limit)"
                )
        else:
            # Só Trancar + Arquivar (Sem tocar no nome)
            await thread.edit(
                locked=True,
                archived=True,
                reason="Finalizado via Bot"
            )
        
    except Exception as e:
        print(f"Erro ao finalizar (Geral): {e}")
        try:
            await thread.edit(locked=True, archived=True, reason="Finalizado via Bot (Fallback)")
        except: pass

# --- EVENTOS DO BOT (RESTAURO DE FUNCIONALIDADE) ---
def setup_events(bot):
    """Configura eventos globais como o bloqueio de mensagens em tópicos fechados"""
    
    @bot.event
    async def on_message(message: discord.Message):
        """Impede mensagens em tópicos trancados, mesmo de admins"""
        if message.author.id == bot.user.id:
            return
        
        # Verifica se é um tópico e se está trancado
        if isinstance(message.channel, discord.Thread) and message.channel.locked:
            try:
                # Apaga a mensagem intrusa
                await message.delete()
                
                # Avisa o utilizador (temporariamente)
                warning = await message.channel.send(
                    f"⛔ {message.author.mention}, este tópico está finalizado! Use **/reabrir** para voltar a interagir."
                )
                await asyncio.sleep(5)
                await warning.delete()
            except Exception as e:
                # Se falhar (ex: sem permissão de gerir mensagens), ignora silenciosamente
                pass

# --- LÓGICA DE EXTRAÇÃO (BACKEND) ---

class ExtractionEngine:
    @staticmethod
    def gerar_texto_toon(contexto: dict, mensagens: list) -> str:
        """Gera texto formatado. Imagens e arquivos viram links."""
        lines = ["contexto:"] + [f"  {k}: {v}" for k, v in contexto.items()]
        if mensagens:
            lines.append(f"mensagens[{len(mensagens)}]{{data,autor,mensagem}}:")
            for m in mensagens:
                txt = m['conteudo'].replace('\n', ' ')
                
                # Formatação de anexos (agora sempre links)
                anexos_formatados = []
                for a in m['anexos']:
                    # Tenta detectar se é imagem visualmente pela URL (apenas para a tag no texto)
                    # Se tiver extensão de imagem, usa [IMAGEM], senão [ARQUIVO]
                    # Nota: 'a' agora é sempre uma URL
                    is_img = any(ext in a.lower() for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'])
                    tag = "IMAGEM" if is_img else "ARQUIVO"
                    
                    anexos_formatados.append(f"[{tag}: {a}]")
                
                anexos_str = " ".join(anexos_formatados)
                full = f"{txt} {anexos_str}".strip()
                lines.append(f"  {m['timestamp_brt']}, {m['autor']['nome']}, {full}")
        return "\n".join(lines)

    @staticmethod
    async def extrair_topico(bot, session, thread, pasta_destino, guild_id):
        """
        Extrai um tópico.
        - Salva apenas LINKS para imagens e arquivos.
        - Não realiza download físico de anexos.
        """
        nome = clean_name(thread.name)
        # pasta_anexos removida, pois não baixaremos mais arquivos físicos
        
        msgs = []
        
        # Tenta recuperar metadados da resolução (Orgão/Categoria)
        try:
            db_path = DataManager.get_path(str(guild_id), "resolucoes.json")
            import json
            if os.path.exists(db_path):
                with open(db_path, 'r', encoding='utf-8') as f: db = json.load(f)
                entry = next((r for r in db if r["thread_id"] == str(thread.id)), None)
            else: entry = None    
            cat = entry["categoria"] if entry else "N/A"
            orgao_val = entry.get("orgao", "N/A") if entry else "N/A"
        except: cat = "Erro"; orgao_val = "Erro"

        # Itera mensagens
        async for m in thread.history(limit=None, oldest_first=True):
            # Ignora mensagens do próprio BOT
            if m.author.id == bot.user.id: 
                continue
            
            paths_or_links = []
            if m.attachments:
                for a in m.attachments:
                    # ALTERAÇÃO: Agora pegamos SEMPRE a URL, independente se é imagem ou arquivo
                    paths_or_links.append(a.url)

            msgs.append({
                "timestamp_brt": m.created_at.astimezone(BRT_OFFSET).strftime("%Y-%m-%d %H:%M:%S"),
                "autor": {"nome": m.author.name},
                "conteudo": m.content,
                "anexos": paths_or_links
            })

        if msgs:
            ctx = {"origem": thread.parent.name if thread.parent else "N/A", "nome": thread.name, "orgao": orgao_val, "categoria": cat, "id": str(thread.id)}
            
            # Salva apenas o arquivo de texto
            with open(os.path.join(pasta_destino, f"topico_{nome}.txt"), "w", encoding="utf-8") as f:
                f.write(ExtractionEngine.gerar_texto_toon(ctx, msgs))
            
            return True
        return False

async def perform_extraction_guild(bot, guild_id: str, target_channels=None, force_all=False):
    """Executa a extração para UM servidor específico (Guild Sharding)"""
    cfg = get_config(guild_id)
    connected = cfg.get("connected_channels", {})
    channels_obj = []
    
    # Se canais específicos forem passados, usa eles. Se não, usa todos os conectados.
    if target_channels: 
        channels_obj = target_channels
    else:
        for cid in connected:
            ch = bot.get_channel(int(cid))
            if ch: channels_obj.append(ch)

    if not channels_obj: return {"canais": 0, "topicos": 0}, None

    ts_now = datetime.now(BRT_OFFSET)
    # Cria pasta temporária única para este processo
    raiz = f"./temp_backups/{guild_id}_{ts_now.strftime('%H%M%S')}"
    stats = {"canais": 0, "topicos": 0}
    extracted = False

    # Mantemos a sessão caso precisemos no futuro, mas extrair_topico não baixa mais nada
    async with aiohttp.ClientSession() as session:
        for ch in channels_obj:
            cid = str(ch.id)
            last_ts_str = connected.get(cid, {}).get("last_marker_timestamp")
            last_ts = datetime.fromisoformat(last_ts_str) if (last_ts_str and not force_all) else None
            pasta_ch = os.path.join(raiz, clean_name(ch.name))
            
            # Processa Threads Arquivadas
            try:
                threads = [t async for t in ch.archived_threads(limit=None)]
            except:
                print(f"Sem permissão para ler threads em {ch.name}")
                continue

            cnt = 0
            for t in threads:
                if not t.locked: continue # Apenas trancados (resolvidos)
                if not t.archive_timestamp: continue
                # Verifica marcador de tempo
                if last_ts and t.archive_timestamp.astimezone(BRT_OFFSET) <= last_ts.astimezone(BRT_OFFSET): continue
                
                os.makedirs(pasta_ch, exist_ok=True)
                
                # Passa a sessão para o motor de extração
                if await ExtractionEngine.extrair_topico(bot, session, t, pasta_ch, guild_id):
                    cnt += 1
                    extracted = True
            
            if cnt > 0:
                stats["canais"] += 1; stats["topicos"] += cnt
                # Atualiza marcador de tempo APENAS deste canal neste servidor
                def update_marker(data):
                    if "connected_channels" in data and cid in data["connected_channels"]:
                        data["connected_channels"][cid]["last_marker_timestamp"] = ts_now.isoformat()
                    return data
                await update_config(guild_id, update_marker)

    zip_path = None
    if extracted:
        zip_path = shutil.make_archive(raiz, 'zip', raiz)
        shutil.rmtree(raiz)
    elif os.path.exists(raiz): shutil.rmtree(raiz)
    return stats, zip_path

# --- DECORATORS & PERMISSÕES ---

def is_master():
    """Verifica se o usuário tem o cargo de Admin Mestre configurado para o servidor atual"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild: return False
        adm_id = get_setup_id(interaction.guild.id, "id_cargo_adm")
        if not adm_id:
            await interaction.response.send_message("⚠️ Bot não configurado. Use `/iniciar`.", ephemeral=True)
            return False
        has_role = any(role.id == adm_id for role in interaction.user.roles)
        if not has_role: await interaction.response.send_message(f"⛔ Requer cargo <@&{adm_id}>.", ephemeral=True)
        return has_role
    return app_commands.check(predicate)

def check_permission(perm_key: str):
    """Verifica permissões granulares configuradas no servidor atual"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild: return False
        adm_id = get_setup_id(interaction.guild.id, "id_cargo_adm")
        # Admin mestre tem passe livre
        if adm_id and any(r.id == adm_id for r in interaction.user.roles): return True
        
        cfg = get_config(str(interaction.guild.id))
        allowed = cfg.get("perms", {}).get(perm_key, [])
        if any(r.id in allowed for r in interaction.user.roles): return True
        
        await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
        return False
    return app_commands.check(predicate)

# --- LOOPS (TASKS) ---

@tasks.loop(time=time(hour=HORA_BACKUP, minute=MINUTO_BACKUP))
async def daily_extraction_loop():
    """Itera sobre todas as pastas de servidor e executa o backup individualmente"""
    if not _bot_instance: return
    active_guilds = get_all_active_guilds()
    print(f"🔄 Iniciando backup diário para {len(active_guilds)} servidores.")
    
    for guild_id in active_guilds:
        try:
            log_channel_id = get_setup_id(int(guild_id), "id_canal_comandos")
            if not log_channel_id: continue
            
            # Executa extração isolada
            stats, zip_path = await perform_extraction_guild(_bot_instance, guild_id)
            
            ch = _bot_instance.get_channel(log_channel_id)
            if ch:
                if zip_path:
                    await ch.send(f"📦 **Backup Auto**\nNovos: {stats['topicos']}", file=discord.File(zip_path))
                    os.remove(zip_path)
                else: await ch.send("✅ Backup diário: Nada novo.")
        except Exception as e: print(f"❌ Erro backup {guild_id}: {e}")

@tasks.loop(minutes=1)
async def update_countdown_loop():
    """Atualiza a mensagem de contagem regressiva em cada servidor"""
    if not _bot_instance: return
    active_guilds = get_all_active_guilds()
    now = datetime.now(BRT_OFFSET)
    target = now.replace(hour=HORA_BACKUP, minute=MINUTO_BACKUP, second=0)
    if now >= target: target += timedelta(days=1)
    ts = int(target.timestamp())
    txt = f"⏳ Próximo backup automático: <t:{ts}:R>"
    
    for guild_id in active_guilds:
        try:
            cid = get_setup_id(int(guild_id), "id_canal_countdown")
            if not cid: continue
            ch = _bot_instance.get_channel(cid)
            if ch:
                # Tenta editar a última mensagem do bot
                async for m in ch.history(limit=5):
                    if m.author == _bot_instance.user:
                        if m.content != txt: await m.edit(content=txt)
                        return
                # Se não achou, envia nova
                await ch.send(txt)
        except: pass

# --- COMANDOS ---

def setup_commands(bot):
    
    @bot.tree.command(name="iniciar", description="[ADMIN] Configura o bot neste servidor.")
    @app_commands.default_permissions(administrator=True)
    async def iniciar(interaction: discord.Interaction):
        if not interaction.guild: return
        view = PainelSetup(bot, interaction.guild.id)
        embed = discord.Embed(title="🛠️ Setup", description="Configure abaixo:", color=0xFEE75C)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @bot.tree.command(name="painel", description="[MASTER] Painel de Controle.")
    @app_commands.checks.cooldown(1, 10.0)
    @is_master()
    async def painel(interaction: discord.Interaction):
        """Abre o painel principal de controle"""
        # Verifica se está no canal correto
        cmd_channel_id = get_setup_id(interaction.guild.id, "id_canal_comandos")
        
        if cmd_channel_id and interaction.channel_id != cmd_channel_id:
            await interaction.response.send_message(f"❌ Este comando só pode ser usado no canal <#{cmd_channel_id}>.", ephemeral=True)
            return
        
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Este comando não pode ser usado dentro de um tópico.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("🔄 **Inicializando sistema...**", ephemeral=True)
        await asyncio.sleep(1)
        
        embed = build_dashboard_embed(bot, interaction.guild.id)
        view = PainelPrincipal(bot, interaction.guild.id)
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    @bot.tree.command(name="extracao_manual", description="[EXTRACAO] Selecione um canal conectado para backup.")
    @check_permission("extracao_canal")
    async def extracao_manual(interaction: discord.Interaction):
        # 1. Verifica se está no canal de comandos configurado
        cmd_channel_id = get_setup_id(interaction.guild.id, "id_canal_comandos")
        if cmd_channel_id and interaction.channel_id != cmd_channel_id:
            await interaction.response.send_message(f"❌ Comando restrito ao canal <#{cmd_channel_id}>.", ephemeral=True)
            return

        # 2. Busca apenas os canais conectados na configuração
        cfg = get_config(str(interaction.guild.id))
        connected_ids = list(cfg.get("connected_channels", {}).keys())
        
        if not connected_ids:
            await interaction.response.send_message("⚠️ Nenhum canal conectado configurado.", ephemeral=True)
            return

        # 3. Mostra View de Seleção com os canais conectados
        view = ExtractionChannelSelectView(bot, interaction.guild.id, connected_ids)
        await interaction.response.send_message("📂 **Extração Manual**\nSelecione o canal abaixo para extrair apenas ele:", view=view, ephemeral=True)

    @bot.tree.command(name="extracao_tudo", description="[EXTRACAO] Backup de TODOS os canais conectados.")
    @check_permission("extracao_tudo")
    async def extracao_tudo(interaction: discord.Interaction):
        # 1. Verifica se está no canal de comandos configurado
        cmd_channel_id = get_setup_id(interaction.guild.id, "id_canal_comandos")
        if cmd_channel_id and interaction.channel_id != cmd_channel_id:
            await interaction.response.send_message(f"❌ Comando restrito ao canal <#{cmd_channel_id}>.", ephemeral=True)
            return

        await interaction.response.defer()
        
        # Chama a função sem especificar canais alvo = Extrai todos os conectados da config
        stats, zip_path = await perform_extraction_guild(bot, str(interaction.guild.id))
        
        if zip_path:
            await interaction.followup.send(f"📦 **Backup Global**: {stats['topicos']} tópicos de {stats['canais']} canais.", file=discord.File(zip_path))
            os.remove(zip_path)
        else: 
            await interaction.followup.send("✅ Backup Global: Nada novo para extrair.")

    @bot.tree.command(name="resolvido", description="[SUPORTE] Finaliza o chamado.")
    @check_permission("resolvido")
    async def resolvido(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("Use em um tópico.", ephemeral=True)
        if interaction.channel.locked:
            return await interaction.response.send_message("Já está trancado.", ephemeral=True)
        view = PainelResolucao(interaction.guild.id)
        await interaction.response.send_message("📁 Finalizar Chamado:", view=view)

    @bot.tree.command(name="reabrir", description="[SUPORTE] Reabre o tópico.")
    @check_permission("reabrir")
    async def reabrir(interaction: discord.Interaction):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread): return await interaction.response.send_message("Use num tópico.", ephemeral=True)
        
        # 1. Deferir (Avisar o Discord que estamos processando) para evitar erro de timeout
        await interaction.response.defer()

        await apagar_mensagens_antigas_bot(bot, thread, "Chamado Finalizado!")
        await remove_resolution(str(interaction.guild.id), thread.id)
        
        try:
            # 2. Lógica para remover variações de OK
            new_name = thread.name
            prefixes = ["OK - ", "OK ", "[OK] ", "[OK]", "(OK) ", "(OK)"]
            for p in prefixes:
                if new_name.startswith(p):
                    # Remove o prefixo e remove espaços extras do início (strip)
                    new_name = new_name[len(p):].strip()
                    break
            
            # Verifica se o nome REALMENTE mudou antes de enviar o request
            if new_name != thread.name:
                try:
                    # Tenta Renomear + Destrancar com timeout (5 segundos)
                    await asyncio.wait_for(thread.edit(
                        name=new_name, 
                        locked=False, 
                        archived=False, 
                        reason=f"Reaberto por {interaction.user.name}"
                    ), timeout=5.0)
                except asyncio.TimeoutError:
                    # Fallback: Só destranca se o renomear travar
                    await thread.edit(
                        locked=False, 
                        archived=False, 
                        reason=f"Reaberto por {interaction.user.name} (Fallback)"
                    )
                    await interaction.followup.send("🔓 Tópico Reaberto! (Nome mantido por limite do Discord)")
                    return
            else:
                # Se NÃO mudou, só destranca e desarquiva (Economiza rate limit)
                await thread.edit(
                    locked=False, 
                    archived=False, 
                    reason=f"Reaberto por {interaction.user.name}"
                )
            
            await interaction.followup.send("🔓 Tópico Reaberto!")
            
        except Exception as e:
            await interaction.followup.send(f"⚠️ Reaberto, mas com erro (Rate Limit?): {e}")