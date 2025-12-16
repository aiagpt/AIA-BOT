"""
extraction.py - Motor de extração adaptado para Multi-Server (Guild Sharding)
Atualizado: Remove data do formato TOON e usa clean_content para menções.
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
    log_pending_safe, remove_pending_safe, get_pending_data, 
    update_config, get_all_active_guilds,
    BRT_OFFSET, HORA_BACKUP, MINUTO_BACKUP, execute_with_retry as executar_com_retry
)

# Importa as Views atualizadas
from ui_components import (
    PainelSetup, PainelPrincipal, PainelResolucao, ExtractionChannelSelectView,
    ApprovalView, build_dashboard_embed
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
    """
    Callback final da UI de resolução.
    Aplica: Pendência, Embed p/ Aprovação e Renomeia para OK imediatamente.
    """
    thread = interaction.channel
    orgao = selections["orgao"]
    cat = selections["categoria"]
    quem = selections["quem_tratou"]
    
    # 1. Limpa mensagens anteriores
    await apagar_mensagens_antigas_bot(_bot_instance, thread, "Tópico Reaberto!")
    await apagar_mensagens_antigas_bot(_bot_instance, thread, "Solicitação de Aprovação")
    
    # 2. Busca Canal de Aprovação
    approval_channel_id = get_setup_id(int(guild_id), "id_canal_aprovacao")
    
    # Se não tiver canal de aprovação, avisa e cancela
    if not approval_channel_id:
        await interaction.response.send_message("❌ ERRO: Canal de aprovação não configurado! Use `/iniciar`.", ephemeral=True)
        return

    # 3. Salva na lista de PENDÊNCIAS (não extração ainda)
    canal_origem_nome = thread.parent.name if thread.parent else "N/A"
    await log_pending_safe(guild_id, thread.id, thread.name, quem, interaction.user.id, cat, orgao, canal_origem_nome)

    # 4. Envia Embed para o canal de Aprovação
    channel_aprov = _bot_instance.get_channel(approval_channel_id)
    if channel_aprov:
        embed_aprov = discord.Embed(title="⚖️ Solicitação de Aprovação", color=0xFEE75C)
        embed_aprov.add_field(name="Tópico", value=f"{thread.mention}\n`{thread.name}`", inline=False)
        embed_aprov.add_field(name="Canal de Origem", value=canal_origem_nome, inline=True)
        embed_aprov.add_field(name="Solicitante", value=f"<@{interaction.user.id}>", inline=True)
        embed_aprov.add_field(name="Quem Tratou", value=quem, inline=True)
        embed_aprov.add_field(name="Classificação", value=f"{orgao} / {cat}", inline=False)
        embed_aprov.set_footer(text=f"ID: {thread.id}")
        
        # Passa a URL diretamente no construtor para evitar erro 50035
        view = ApprovalView(_bot_instance, guild_id, thread.id, thread.jump_url)
        
        await channel_aprov.send(embed=embed_aprov, view=view)
    else:
        await interaction.response.send_message("❌ Erro: Canal de aprovação não encontrado.", ephemeral=True)
        return

    # 5. Avisa no Tópico (Embed Local) e agenda deleção
    embed_local = discord.Embed(title="🔒 Aguardando Aprovação", description="Este chamado foi enviado para análise.", color=0x95a5a6)
    embed_local.set_footer(text="Aguarde um administrador aprovar para finalizar.")
    
    if interaction.response.is_done(): 
        await interaction.edit_original_response(content=None, embed=embed_local, view=None)
    else: 
        await interaction.response.edit_message(content=None, embed=embed_local, view=None)
    
    # Tarefa para deletar a mensagem de interação após 30s
    async def delete_after_delay():
        await asyncio.sleep(30)
        try:
            await interaction.delete_original_response()
        except: pass
    asyncio.create_task(delete_after_delay())

    # 6. APLICA A REGRA DO OK (Renomear) e TRANCA
    try:
        prefixes = ["OK - ", "OK ", "[OK] ", "[OK]", "(OK) ", "(OK)"]
        new_name = thread.name
        has_prefix = any(new_name.startswith(p) for p in prefixes)
        
        if not has_prefix:
            new_name = f"OK - {new_name}"
        
        # Tenta renomear e trancar
        if new_name != thread.name:
            try:
                await asyncio.wait_for(thread.edit(
                    name=new_name,
                    locked=True,
                    archived=False,
                    reason="Aguardando Aprovação (Renomeado)"
                ), timeout=5.0)
            except asyncio.TimeoutError:
                await thread.edit(locked=True, archived=False, reason="Aguardando Aprovação (Fallback)")
        else:
            await thread.edit(locked=True, archived=False, reason="Aguardando Aprovação")
            
    except Exception as e:
        print(f"Erro ao renomear thread na solicitação: {e}")

# --- FUNÇÕES DE APROVAÇÃO/REJEIÇÃO ---

async def confirmar_aprovacao(bot, interaction: discord.Interaction, guild_id: str, thread_id_str: str):
    """Ação do botão 'Aprovar'"""
    
    # 1. Checa permissão
    setup_adm = get_setup_id(int(guild_id), "id_cargo_adm")
    perms = get_config(guild_id).get("perms", {}).get("aprovar", [])
    user_roles = [r.id for r in interaction.user.roles]
    has_perm = (setup_adm in user_roles) or any(rid in user_roles for rid in perms)
    
    if not has_perm:
        await interaction.response.send_message("⛔ Você não tem permissão para aprovar.", ephemeral=True)
        return

    # 2. Carrega dados da pendência
    data = await get_pending_data(guild_id, int(thread_id_str))
    if not data:
        await interaction.response.send_message("❌ Erro: Dados da pendência não encontrados (já aprovado?).", ephemeral=True)
        return

    await interaction.response.defer()

    # 3. Move para Resolvidos (Habilita Extração)
    await log_resolution_safe(guild_id, int(thread_id_str), data["thread_nome"], 
                              data["resolvido_por"], int(data["resolvido_por_id"]), 
                              data["categoria"], data["orgao"])
    
    # 4. Remove da Pendência
    await remove_pending_safe(guild_id, int(thread_id_str))

    # 5. Atualiza Mensagem de Aprovação (Embed Verde)
    try:
        embed = interaction.message.embeds[0]
        embed.title = "✅ Chamado Aprovado"
        embed.color = 0x2ecc71
        embed.add_field(name="Aprovado por", value=interaction.user.mention, inline=False)
        await interaction.message.edit(embed=embed, view=None)
    except: pass

    # 6. Finaliza tópico (Arquiva e garante OK)
    thread = bot.get_channel(int(thread_id_str))
    if thread:
        try:
            # Avisa no tópico (apaga em 30s)
            await thread.send(f"✅ **Aprovado!** Chamado finalizado e pronto para backup.", delete_after=30)
            
            # Garante prefixo OK e arquiva
            prefixes = ["OK - ", "OK ", "[OK] ", "[OK]", "(OK) ", "(OK)"]
            new_name = thread.name
            has_prefix = any(new_name.startswith(p) for p in prefixes)
                
            if not has_prefix:
                new_name = f"OK - {new_name}"
            
            if new_name != thread.name:
                try:
                    await asyncio.wait_for(thread.edit(
                        name=new_name,
                        locked=True,
                        archived=True,
                        reason=f"Aprovado por {interaction.user.name}"
                    ), timeout=5.0)
                except asyncio.TimeoutError:
                    await thread.edit(locked=True, archived=True)
            else:
                await thread.edit(locked=True, archived=True)
        except Exception as e:
            print(f"Erro ao manipular thread aprovada: {e}")
    
    await interaction.followup.send("✅ Aprovado com sucesso.", ephemeral=True)


async def rejeitar_aprovacao(bot, interaction: discord.Interaction, guild_id: str, thread_id_str: str):
    """Ação do botão 'Reprovar'"""
    
    # 1. Checa permissão
    setup_adm = get_setup_id(int(guild_id), "id_cargo_adm")
    perms = get_config(guild_id).get("perms", {}).get("aprovar", [])
    user_roles = [r.id for r in interaction.user.roles]
    has_perm = (setup_adm in user_roles) or any(rid in user_roles for rid in perms)
    
    if not has_perm:
        await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
        return

    await interaction.response.defer()

    # 2. Remove da Pendência (Não vai pra extração)
    await remove_pending_safe(guild_id, int(thread_id_str))

    # 3. Atualiza Mensagem (Embed Vermelho)
    try:
        embed = interaction.message.embeds[0]
        embed.title = "🚫 Reprovado (Sem Extração)"
        embed.description = "Tópico mantido fechado, mas removido da lista de backup."
        embed.color = 0xe74c3c
        embed.add_field(name="Reprovado por", value=interaction.user.mention, inline=False)
        await interaction.message.edit(embed=embed, view=None)
    except: pass

    # 4. Mantém o Tópico FECHADO (Trancado/Arquivado) e Garante OK (conforme solicitado)
    thread = bot.get_channel(int(thread_id_str))
    if thread:
        try:
            # Avisa no tópico (apaga em 30s)
            await thread.send(
                f"🚫 **Encerrado (Sem Backup)!** Negado por {interaction.user.mention}.", 
                delete_after=30
            )
            
            # Aplica OK mesmo reprovado (pedido do usuário)
            prefixes = ["OK - ", "OK ", "[OK] ", "[OK]", "(OK) ", "(OK)"]
            new_name = thread.name
            has_prefix = any(new_name.startswith(p) for p in prefixes)
            
            if not has_prefix:
                new_name = f"OK - {new_name}"
            
            if new_name != thread.name:
                try:
                    await asyncio.wait_for(thread.edit(
                        name=new_name,
                        locked=True,
                        archived=True,
                        reason="Reprovado para extração (Renomeado)"
                    ), timeout=5.0)
                except asyncio.TimeoutError:
                    await thread.edit(locked=True, archived=True, reason="Reprovado (Fallback)")
            else:
                await thread.edit(locked=True, archived=True, reason="Reprovado para extração")
                
        except: pass
    
    await interaction.followup.send("🚫 Reprovado e mantido fechado.", ephemeral=True)


# --- EVENTOS DO BOT ---
def setup_events(bot):
    
    @bot.event
    async def on_message(message: discord.Message):
        if message.author.id == bot.user.id: return
        
        # Impede mensagens em tópicos trancados
        if isinstance(message.channel, discord.Thread) and message.channel.locked:
            try:
                await message.delete()
                warning = await message.channel.send(
                    f"⛔ {message.author.mention}, este tópico está finalizado ou em análise! Use **/reabrir** (se permitido).",
                    delete_after=5
                )
            except: pass

# --- LÓGICA DE EXTRAÇÃO (BACKEND) ---

class ExtractionEngine:
    @staticmethod
    def gerar_texto_toon(contexto: dict, mensagens: list) -> str:
        lines = ["contexto:"] + [f"  {k}: {v}" for k, v in contexto.items()]
        if mensagens:
            # Modificado: Retirada a tag 'data' do cabeçalho
            lines.append(f"mensagens[{len(mensagens)}]{{autor,mensagem}}:")
            for m in mensagens:
                txt = m['conteudo'].replace('\n', ' ')
                anexos_formatados = []
                for a in m['anexos']:
                    is_img = any(ext in a.lower() for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'])
                    tag = "IMAGEM" if is_img else "ARQUIVO"
                    anexos_formatados.append(f"[{tag}: {a}]")
                anexos_str = " ".join(anexos_formatados)
                full = f"{txt} {anexos_str}".strip()
                # Modificado: Retirado m['timestamp_brt'] da string final
                lines.append(f"  {m['autor']['nome']}, {full}")
        return "\n".join(lines)

    @staticmethod
    async def extrair_topico(bot, session, thread, pasta_destino, guild_id):
        nome = clean_name(thread.name)
        msgs = []
        
        # Recupera metadados da RESOLUÇÃO
        # Se não estiver em resolucoes.json, retorna False (não extrai)
        try:
            db_path = DataManager.get_path(str(guild_id), "resolucoes.json")
            import json
            if os.path.exists(db_path):
                with open(db_path, 'r', encoding='utf-8') as f: db = json.load(f)
                entry = next((r for r in db if r["thread_id"] == str(thread.id)), None)
            else: entry = None
            
            # SE NÃO TIVER ENTRY, SIGNIFICA QUE NÃO FOI APROVADO PARA EXTRAÇÃO
            if not entry:
                return False

            cat = entry["categoria"]
            orgao_val = entry.get("orgao", "N/A")
        except: 
            return False # Erro na leitura ou sem permissão

        async for m in thread.history(limit=None, oldest_first=True):
            if m.author.id == bot.user.id: continue
            paths_or_links = [a.url for a in m.attachments] if m.attachments else []
            
            # Modificado: Usando clean_content para substituir menções por nomes (@Pessoa)
            conteudo_limpo = m.clean_content

            msgs.append({
                "timestamp_brt": m.created_at.astimezone(BRT_OFFSET).strftime("%Y-%m-%d %H:%M:%S"),
                "autor": {"nome": m.author.display_name}, # Modificado: Usando display_name para ser mais amigável
                "conteudo": conteudo_limpo,
                "anexos": paths_or_links
            })

        if msgs:
            ctx = {"origem": thread.parent.name if thread.parent else "N/A", "nome": thread.name, "orgao": orgao_val, "categoria": cat, "id": str(thread.id)}
            with open(os.path.join(pasta_destino, f"topico_{nome}.txt"), "w", encoding="utf-8") as f:
                f.write(ExtractionEngine.gerar_texto_toon(ctx, msgs))
            return True
        return False

async def perform_extraction_guild(bot, guild_id: str, target_channels=None, force_all=False):
    cfg = get_config(guild_id)
    connected = cfg.get("connected_channels", {})
    channels_obj = []
    
    if target_channels: 
        channels_obj = target_channels
    else:
        for cid in connected:
            ch = bot.get_channel(int(cid))
            if ch: channels_obj.append(ch)

    if not channels_obj: return {"canais": 0, "topicos": 0}, None

    ts_now = datetime.now(BRT_OFFSET)
    raiz = f"./temp_backups/{guild_id}_{ts_now.strftime('%H%M%S')}"
    stats = {"canais": 0, "topicos": 0}
    extracted = False

    async with aiohttp.ClientSession() as session:
        for ch in channels_obj:
            cid = str(ch.id)
            last_ts_str = connected.get(cid, {}).get("last_marker_timestamp")
            last_ts = datetime.fromisoformat(last_ts_str) if (last_ts_str and not force_all) else None
            pasta_ch = os.path.join(raiz, clean_name(ch.name))
            
            try:
                threads = [t async for t in ch.archived_threads(limit=None)]
            except: continue

            cnt = 0
            for t in threads:
                # Extrai APENAS se estiver trancado (resolvido/aprovado) e arquivado
                if not t.locked or not t.archive_timestamp: continue
                if last_ts and t.archive_timestamp.astimezone(BRT_OFFSET) <= last_ts.astimezone(BRT_OFFSET): continue
                
                os.makedirs(pasta_ch, exist_ok=True)
                # A função extrair_topico agora verifica se está em resolucoes.json
                if await ExtractionEngine.extrair_topico(bot, session, t, pasta_ch, guild_id):
                    cnt += 1
                    extracted = True
            
            if cnt > 0:
                stats["canais"] += 1; stats["topicos"] += cnt
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
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild: return False
        adm_id = get_setup_id(interaction.guild.id, "id_cargo_adm")
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
    if not _bot_instance: return
    active_guilds = get_all_active_guilds()
    print(f"🔄 Iniciando backup diário para {len(active_guilds)} servidores.")
    
    for guild_id in active_guilds:
        try:
            log_channel_id = get_setup_id(int(guild_id), "id_canal_comandos")
            if not log_channel_id: continue
            
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
                async for m in ch.history(limit=5):
                    if m.author == _bot_instance.user:
                        if m.content != txt: await m.edit(content=txt)
                        return
                await ch.send(txt)
        except: pass

# --- COMANDOS ---

def setup_commands(bot):
    
    @bot.tree.command(name="iniciar", description="[ADMIN] Configura o bot neste servidor.")
    @app_commands.default_permissions(administrator=True)
    async def iniciar(interaction: discord.Interaction):
        if not interaction.guild: return
        view = PainelSetup(bot, interaction.guild.id)
        embed = discord.Embed(title="🛠️ Setup", description="Configure os canais e cargos:", color=0xFEE75C)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @bot.tree.command(name="painel", description="[MASTER] Painel de Controle.")
    @app_commands.checks.cooldown(1, 10.0)
    @is_master()
    async def painel(interaction: discord.Interaction):
        cmd_channel_id = get_setup_id(interaction.guild.id, "id_canal_comandos")
        if cmd_channel_id and interaction.channel_id != cmd_channel_id:
            await interaction.response.send_message(f"❌ Use no canal <#{cmd_channel_id}>.", ephemeral=True)
            return
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Não use em tópicos.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        embed = build_dashboard_embed(bot, interaction.guild.id)
        view = PainelPrincipal(bot, interaction.guild.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @bot.tree.command(name="extracao_manual", description="[EXTRACAO] Selecione um canal para backup.")
    @check_permission("extracao_canal")
    async def extracao_manual(interaction: discord.Interaction):
        cmd_channel_id = get_setup_id(interaction.guild.id, "id_canal_comandos")
        if cmd_channel_id and interaction.channel_id != cmd_channel_id:
            await interaction.response.send_message(f"❌ Use no canal <#{cmd_channel_id}>.", ephemeral=True)
            return
        cfg = get_config(str(interaction.guild.id))
        connected_ids = list(cfg.get("connected_channels", {}).keys())
        if not connected_ids:
            await interaction.response.send_message("⚠️ Nenhum canal conectado.", ephemeral=True)
            return
        view = ExtractionChannelSelectView(bot, interaction.guild.id, connected_ids)
        await interaction.response.send_message("📂 **Extração Manual**", view=view, ephemeral=True)

    @bot.tree.command(name="extracao_tudo", description="[EXTRACAO] Backup de TODOS os canais.")
    @check_permission("extracao_tudo")
    async def extracao_tudo(interaction: discord.Interaction):
        cmd_channel_id = get_setup_id(interaction.guild.id, "id_canal_comandos")
        if cmd_channel_id and interaction.channel_id != cmd_channel_id:
            await interaction.response.send_message(f"❌ Use no canal <#{cmd_channel_id}>.", ephemeral=True)
            return
        await interaction.response.defer()
        stats, zip_path = await perform_extraction_guild(bot, str(interaction.guild.id))
        if zip_path:
            await interaction.followup.send(f"📦 **Backup Global**: {stats['topicos']} tópicos.", file=discord.File(zip_path))
            os.remove(zip_path)
        else: await interaction.followup.send("✅ Backup Global: Nada novo.")

    @bot.tree.command(name="resolvido", description="[SUPORTE] Solicita finalização e aprovação.")
    @check_permission("resolvido")
    async def resolvido(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("Use em um tópico.", ephemeral=True)
        if interaction.channel.locked:
            return await interaction.response.send_message("Já está trancado.", ephemeral=True)
        view = PainelResolucao(interaction.guild.id)
        await interaction.response.send_message("📁 **Solicitação de Encerramento**:", view=view)

    @bot.tree.command(name="reabrir", description="[SUPORTE] Reabre o tópico.")
    @check_permission("reabrir")
    async def reabrir(interaction: discord.Interaction):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread): return await interaction.response.send_message("Use num tópico.", ephemeral=True)
        await interaction.response.defer()
        
        # Se for pendente, remove da lista
        await remove_pending_safe(str(interaction.guild.id), thread.id)
        # Se for resolvido, remove da lista
        await remove_resolution(str(interaction.guild.id), thread.id)
        
        await apagar_mensagens_antigas_bot(bot, thread, "Chamado Aprovado")
        await apagar_mensagens_antigas_bot(bot, thread, "Chamado Reprovado")

        try:
            # Tenta remover o OK
            new_name = thread.name
            prefixes = ["OK - ", "OK ", "[OK] ", "[OK]", "(OK) ", "(OK)"]
            for p in prefixes:
                if new_name.startswith(p):
                    new_name = new_name[len(p):].strip()
                    break
            
            if new_name != thread.name:
                try:
                    await asyncio.wait_for(thread.edit(name=new_name, locked=False, archived=False), timeout=5.0)
                except:
                    await thread.edit(locked=False, archived=False)
            else:
                await thread.edit(locked=False, archived=False)
            
            await interaction.followup.send("🔓 Tópico Reaberto e removido das pendências/resoluções.")
        except Exception as e:
            await interaction.followup.send(f"⚠️ Reaberto com erro: {e}")