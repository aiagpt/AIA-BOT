"""
AMANDa - Bot de Suporte Discord com Extração e Rastreamento de Tópicos
Gerencia canais, categorias, permissões e arquivo automático de discussões.
"""

# --- IMPORTS ---
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import json
import os
import aiohttp
import shutil
import asyncio
import traceback
import re
from datetime import datetime, timezone, time, timedelta
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURAÇÃO GERAL ---
HORA_BACKUP = 14       
MINUTO_BACKUP = 4  
BRT_OFFSET = timezone(timedelta(hours=-3))

# Arquivos de dados
DATA_FILES = {
    "config": "config.json",
    "categorias": "categorias.json",
    "db": "resolucoes_db.json",
    "logs": "logs.json"
}

# --- LOCKS PARA OPERAÇÕES ASYNC SEGURAS ---
ASYNC_LOCKS = {
    "config": asyncio.Lock(),
    "categorias": asyncio.Lock(),
    "db": asyncio.Lock(),
    "logs": asyncio.Lock()
}

# --- CONFIGURAÇÃO DO BOT ---
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
countdown_message_id = None

# --- GERENCIAMENTO DE DADOS (JSON) ---

class DataManager:
    """Gerenciador centralizado de operações com arquivos JSON"""
    
    @staticmethod
    def load_json(file_path: str, default_data: dict) -> dict:
        """Carrega dados JSON ou retorna padrão"""
        if not os.path.exists(file_path):
            DataManager.save_sync(file_path, default_data)
            return default_data
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return default_data

    @staticmethod
    def save_sync(file_path: str, data: dict) -> None:
        """Salva dados JSON sincronamente"""
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    @staticmethod
    async def save_async(file_path: str, data: dict) -> None:
        """Salva dados JSON asincronamente"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, DataManager.save_sync, file_path, data)

    @staticmethod
    def get_lock(key: str) -> asyncio.Lock:
        """Retorna lock específica para operação"""
        return ASYNC_LOCKS.get(key, asyncio.Lock())


# --- GERENCIAMENTO SEGURO DE DADOS ---

async def registrar_log_safe(acao: str, usuario: str, detalhes: str) -> None:
    """Registra ação no log com rotação automática"""
    async with DataManager.get_lock("logs"):
        logs_file = DATA_FILES["logs"]
        logs = DataManager.load_json(logs_file, [])
        
        # Rotação de arquivo se exceder 5MB
        if os.path.exists(logs_file) and os.path.getsize(logs_file) > 5 * 1024 * 1024:
            novo_nome = f"logs_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            os.rename(logs_file, novo_nome)
            logs = []
        
        logs.append({
            "timestamp": datetime.now(BRT_OFFSET).isoformat(),
            "acao": acao,
            "usuario": usuario,
            "detalhes": detalhes
        })
        await DataManager.save_async(logs_file, logs)


async def update_categories_safe(callback_modification) -> dict:
    """Atualiza categorias com callback seguro"""
    async with DataManager.get_lock("categorias"):
        data = DataManager.load_json(DATA_FILES["categorias"], {})
        data = callback_modification(data)
        await DataManager.save_async(DATA_FILES["categorias"], data)
        return data


async def log_resolution_safe(thread_id: int, thread_name: str, resolvido_por: str, 
                              resolvido_por_id: int, categoria: str, orgao: str) -> None:
    """Registra resolução de tópico no banco de dados"""
    async with DataManager.get_lock("db"):
        db = DataManager.load_json(DATA_FILES["db"], [])
        str_id = str(thread_id)
        entry = next((i for i in db if i.get("thread_id") == str_id), None)
        
        new_data = {
            "data": datetime.now(BRT_OFFSET).isoformat(),
            "thread_id": str_id,
            "thread_nome": thread_name,
            "resolvido_por": resolvido_por,
            "resolvido_por_id": str(resolvido_por_id),
            "orgao": orgao,
            "categoria": categoria
        }
        
        if entry:
            entry.update(new_data)
        else:
            db.append(new_data)
        
        await DataManager.save_async(DATA_FILES["db"], db)

# --- CONFIGURAÇÃO ---

def get_config() -> dict:
    """Obtém configuração com validação de chaves"""
    default_cfg = {
        "setup": {
            "id_cargo_adm": None,
            "id_canal_comandos": None,
            "id_canal_countdown": None
        },
        "connected_channels": {},
        "perms": {
            "extracao_canal": [],
            "extracao_tudo": [],
            "reabrir": [],
            "resolvido": []
        }
    }
    data = DataManager.load_json(DATA_FILES["config"], default_cfg)
    
    # Garante estrutura completa (migração)
    for key in ["perms", "setup"]:
        if key not in data:
            data[key] = default_cfg[key]
    
    return data


def get_categories() -> dict:
    """Obtém categorias com validação automática"""
    default_structure = {
        "orgaos": {},
        "equipes": ["Dev", "Processos"]
    }
    data = DataManager.load_json(DATA_FILES["categorias"], default_structure)
    changed = False
    
    # Migração: remover "categorias" antiga
    if "categorias" in data and "orgaos" not in data:
        data["orgaos"] = default_structure["orgaos"]
        changed = True
    
    # Garantir chaves essenciais
    for key in ["equipes", "orgaos"]:
        if key not in data:
            data[key] = default_structure[key]
            changed = True
    
    if changed:
        DataManager.save_sync(DATA_FILES["categorias"], data)
    
    return data


def get_setup_id(key: str) -> int:
    """Helper para obter ID de setup"""
    cfg = get_config()
    val = cfg.get("setup", {}).get(key)
    return int(val) if val else None

# --- UTILITÁRIOS GERAIS ---

def sanitize_input(texto: str, max_len: int = 50) -> str:
    """Remove caracteres especiais e limita tamanho"""
    if not texto:
        return ""
    limpo = re.sub(r'[^\w\s\-\.]', '', texto)
    return limpo[:max_len].strip()


def clean_name(nome: str) -> str:
    """Limpa nome de arquivo/pasta"""
    return "".join(c for c in nome if c.isalnum() or c in ('-', '_', ' ')).strip().replace(' ', '_')


async def enviar_log_discord(titulo: str, descricao: str, cor: int, campos=None) -> None:
    """Envia log como embed no Discord"""
    try:
        channel_id = get_setup_id("id_canal_comandos")
        if not channel_id:
            return
        
        ch = bot.get_channel(channel_id)
        if not ch:
            return
        
        embed = discord.Embed(title=titulo, description=descricao, color=cor)
        embed.timestamp = datetime.now(BRT_OFFSET)
        
        if campos:
            for nome, valor in campos:
                embed.add_field(name=nome, value=valor, inline=True)
        
        await ch.send(embed=embed)
    except:
        pass


async def executar_com_retry(funcao, *args, tentativas: int = 3, delay: int = 5, **kwargs):
    """Executa função com retry automático"""
    ultimo_erro = None
    for i in range(tentativas):
        try:
            return await funcao(*args, **kwargs)
        except Exception as e:
            ultimo_erro = e
            print(f"⚠️ Erro na tentativa {i+1}/{tentativas}: {e}")
            if i < tentativas - 1:
                await asyncio.sleep(delay)
            else:
                await enviar_log_discord(
                    "❌ Erro Crítico",
                    f"Falha após {tentativas} tentativas.",
                    0xe74c3c,
                    [("Erro", str(e)), ("Função", funcao.__name__)]
                )
                raise ultimo_erro


def log_resolution(thread_id: int, thread_name: str, resolvido_por_nome: str, 
                  resolvido_por_id: int, categoria: str, orgao: str) -> None:
    """Enfileira log de resolução de forma assíncrona"""
    asyncio.create_task(
        log_resolution_safe(thread_id, thread_name, resolvido_por_nome, resolvido_por_id, categoria, orgao)
    )


def remove_resolution(thread_id: int) -> bool:
    """Remove entrada de resolução do banco"""
    db = DataManager.load_json(DATA_FILES["db"], [])
    str_id = str(thread_id)
    novo_db = [entry for entry in db if entry.get("thread_id") != str_id]
    
    if len(novo_db) < len(db):
        DataManager.save_sync(DATA_FILES["db"], novo_db)
        return True
    
    return False

# --- PERMISSÕES & CONTROLES ---

def is_master():
    """Verifica se usuário é admin mestre"""
    async def predicate(interaction: discord.Interaction) -> bool:
        adm_id = get_setup_id("id_cargo_adm")
        if not adm_id:
            await interaction.response.send_message("⚠️ O bot não foi configurado! Use `/iniciar`.", ephemeral=True)
            return False
        
        has_role = any(role.id == adm_id for role in interaction.user.roles)
        if not has_role:
            await interaction.response.send_message(f"⛔ Apenas o Admin Mestre <@&{adm_id}> pode usar este painel.", ephemeral=True)
        
        return has_role
    
    return app_commands.check(predicate)


def check_permission(command_key: str):
    """Verifica permissão para comando específico"""
    async def predicate(interaction: discord.Interaction) -> bool:
        adm_id = get_setup_id("id_cargo_adm")
        
        # Admin mestre tem acesso total
        if adm_id and any(role.id == adm_id for role in interaction.user.roles):
            return True
        
        # Verifica permissões específicas
        cfg = get_config()
        allowed_roles = cfg.get("perms", {}).get(command_key, [])
        user_roles = [r.id for r in interaction.user.roles]
        
        if not any(rid in user_roles for rid in allowed_roles):
            await interaction.response.send_message(f"⛔ Sem permissão para usar `/{command_key}`.", ephemeral=True)
            return False
        
        return True
    
    return app_commands.check(predicate)


def check_valid_channel() -> app_commands.check:
    """Verifica se comando está sendo usado em local válido"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Este comando não pode ser usado dentro de um tópico.", ephemeral=True)
            return False
        
        cmd_channel_id = get_setup_id("id_canal_comandos")
        if cmd_channel_id and interaction.channel_id != cmd_channel_id:
            await interaction.response.send_message(f"❌ Este comando só pode ser usado no canal <#{cmd_channel_id}>.", ephemeral=True)
            return False
        
        return True
    
    return app_commands.check(predicate)


async def apagar_mensagens_antigas_bot(thread: discord.Thread, texto_para_buscar: str) -> None:
    """Remove mensagens antigas do bot com base no texto"""
    try:
        async for msg in thread.history(limit=50):
            if msg.author.id != bot.user.id:
                continue
            
            should_delete = False
            
            # Verifica no conteúdo
            if msg.content and texto_para_buscar in msg.content:
                should_delete = True
            
            # Verifica nos embeds
            if not should_delete and msg.embeds:
                for embed in msg.embeds:
                    if embed.title and texto_para_buscar in embed.title:
                        should_delete = True
                        break
            
            if should_delete:
                await msg.delete()
                await asyncio.sleep(0.5)
    except:
        pass

# --- BASE CLASSES PARA UI REUTILIZÁVEL ---

class BaseView(ui.View):
    """Classe base para views com padrões comuns"""
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance


class BaseSelectionView(BaseView):
    """Base para views com seletor simples"""
    def __init__(self, bot_instance, items_list: list, placeholder: str, custom_id: str):
        super().__init__(bot_instance)
        self.items_list = items_list
        self.add_selection(items_list, placeholder, custom_id)
    
    def add_selection(self, items_list: list, placeholder: str, custom_id: str):
        """Adiciona seletor e botões padrão"""
        if items_list:
            options = [discord.SelectOption(label=item, value=item, emoji="🗑️") for item in items_list[:25]]
            select = ui.Select(placeholder=placeholder, options=options, custom_id=custom_id)
            select.callback = self.on_select
            self.add_item(select)
        
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary, row=1, emoji="✖️")
        btn_cancel.callback = self.on_cancel
        self.add_item(btn_cancel)
    
    async def on_select(self, interaction: discord.Interaction):
        """Override em subclasses"""
        pass
    
    async def on_cancel(self, interaction: discord.Interaction):
        """Override em subclasses"""
        pass
class PainelSetup(BaseView):
    """Painel inicial de configuração"""
    
    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.selections = {
            "id_cargo_adm": get_setup_id("id_cargo_adm"),
            "id_canal_comandos": get_setup_id("id_canal_comandos"),
            "id_canal_countdown": get_setup_id("id_canal_countdown")
        }

    @ui.select(cls=ui.RoleSelect, placeholder="👑 Selecione o Cargo de ADMIN MESTRE", min_values=1, max_values=1, row=0)
    async def select_adm(self, interaction: discord.Interaction, select: ui.RoleSelect):
        self.selections["id_cargo_adm"] = select.values[0].id
        await interaction.response.defer()

    @ui.select(cls=ui.ChannelSelect, placeholder="💻 Canal do Painel/Comandos", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=1)
    async def select_cmd_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        self.selections["id_canal_comandos"] = select.values[0].id
        await interaction.response.defer()

    @ui.select(cls=ui.ChannelSelect, placeholder="⏳ Canal do Cronômetro/Backup", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=2)
    async def select_time_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        self.selections["id_canal_countdown"] = select.values[0].id
        await interaction.response.defer()

    @ui.button(label="Salvar Configuração", style=discord.ButtonStyle.success, emoji="💾", row=3)
    async def btn_save(self, interaction: discord.Interaction, button: ui.Button):
        if not all(self.selections.values()):
            await interaction.response.send_message("⚠️ Por favor, selecione todas as opções antes de salvar.", ephemeral=True)
            return

        async with DataManager.get_lock("config"):
            cfg = get_config()
            cfg["setup"]["id_cargo_adm"] = self.selections["id_cargo_adm"]
            cfg["setup"]["id_canal_comandos"] = self.selections["id_canal_comandos"]
            cfg["setup"]["id_canal_countdown"] = self.selections["id_canal_countdown"]
            await DataManager.save_async(DATA_FILES["config"], cfg)
        
        embed = discord.Embed(title="✅ Configuração Inicial Concluída!", color=0x2ecc71)
        embed.add_field(name="Cargo Admin", value=f"<@&{self.selections['id_cargo_adm']}>")
        embed.add_field(name="Canal Comandos", value=f"<#{self.selections['id_canal_comandos']}>")
        embed.add_field(name="Canal Cronômetro", value=f"<#{self.selections['id_canal_countdown']}>")
        embed.set_footer(text="Agora você pode usar /painel")
        
        await interaction.response.edit_message(content=None, embed=embed, view=None)


class PainelSetup(ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.selections = {
            "id_cargo_adm": get_setup_id("id_cargo_adm"),
            "id_canal_comandos": get_setup_id("id_canal_comandos"),
            "id_canal_countdown": get_setup_id("id_canal_countdown")
        }
        self.update_components()

    def update_components(self):
        # A lógica aqui é apenas visual, os selects mantêm o estado
        pass

    @ui.select(cls=ui.RoleSelect, placeholder="👑 Selecione o Cargo de ADMIN MESTRE", min_values=1, max_values=1, row=0)
    async def select_adm(self, interaction: discord.Interaction, select: ui.RoleSelect):
        self.selections["id_cargo_adm"] = select.values[0].id
        await interaction.response.defer()

    @ui.select(cls=ui.ChannelSelect, placeholder="💻 Canal do Painel/Comandos", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=1)
    async def select_cmd_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        self.selections["id_canal_comandos"] = select.values[0].id
        await interaction.response.defer()

    @ui.select(cls=ui.ChannelSelect, placeholder="⏳ Canal do Cronômetro/Backup", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=2)
    async def select_time_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        self.selections["id_canal_countdown"] = select.values[0].id
        await interaction.response.defer()

    @ui.button(label="Salvar Configuração", style=discord.ButtonStyle.success, emoji="💾", row=3)
    async def btn_save(self, interaction: discord.Interaction, button: ui.Button):
        if not all(self.selections.values()):
            await interaction.response.send_message("⚠️ Por favor, selecione todas as opções antes de salvar.", ephemeral=True)
            return

        async with DataManager.get_lock("config"):
            cfg = get_config()
            cfg["setup"]["id_cargo_adm"] = self.selections["id_cargo_adm"]
            cfg["setup"]["id_canal_comandos"] = self.selections["id_canal_comandos"]
            cfg["setup"]["id_canal_countdown"] = self.selections["id_canal_countdown"]
            await DataManager.save_async(DATA_FILES["config"], cfg)
        
        embed = discord.Embed(title="✅ Configuração Inicial Concluída!", color=0x2ecc71)
        embed.add_field(name="Cargo Admin", value=f"<@&{self.selections['id_cargo_adm']}>")
        embed.add_field(name="Canal Comandos", value=f"<#{self.selections['id_canal_comandos']}>")
        embed.add_field(name="Canal Cronômetro", value=f"<#{self.selections['id_canal_countdown']}>")
        embed.set_footer(text="Agora você pode usar /painel")
        
        await interaction.response.edit_message(content=None, embed=embed, view=None)

# --- VIEWS DE GERENCIAMENTO (EXCLUSÃO) ---

class ExcluirOrgaoView(BaseSelectionView):
    """View para exclusão de órgão"""
    
    def __init__(self, bot):
        data = get_categories()
        orgaos = sorted(list(data.get("orgaos", {}).keys()))
        super().__init__(bot, orgaos, "Selecione o Orgão para APAGAR", "del_orgao")

    async def on_select(self, interaction: discord.Interaction):
        valor = interaction.data['values'][0]
        
        def remove(data):
            if valor in data["orgaos"]:
                del data["orgaos"][valor]
            return data
        
        await update_categories_safe(remove)
        await interaction.response.edit_message(
            content=f"✅ Orgão **{valor}** excluído com sucesso!",
            view=PainelGerenciamento(self.bot)
        )

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot),
            view=PainelGerenciamento(self.bot)
        )


class ExcluirEquipeView(BaseSelectionView):
    """View para exclusão de equipe"""
    
    def __init__(self, bot):
        data = get_categories()
        equipes = sorted(data.get("equipes", []))
        super().__init__(bot, equipes, "Selecione a Equipe para APAGAR", "del_equipe")

    async def on_select(self, interaction: discord.Interaction):
        valor = interaction.data['values'][0]
        
        def remove(data):
            if valor in data["equipes"]:
                data["equipes"].remove(valor)
            return data
        
        await update_categories_safe(remove)
        await interaction.response.edit_message(
            content=f"✅ Equipe **{valor}** excluída com sucesso!",
            view=PainelGerenciamento(self.bot)
        )

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot),
            view=PainelGerenciamento(self.bot)
        )


class ExcluirCategoriaStep2View(BaseSelectionView):
    """View para exclusão de categoria (passo 2)"""
    
    def __init__(self, bot, orgao):
        self.orgao = orgao
        data = get_categories()
        cats = sorted(data.get("orgaos", {}).get(orgao, []))
        super().__init__(bot, cats, f"Apagar categoria de {orgao}...", "del_cat_final")

    async def on_select(self, interaction: discord.Interaction):
        cat_val = interaction.data['values'][0]
        
        def remove(data):
            if self.orgao in data["orgaos"] and cat_val in data["orgaos"][self.orgao]:
                data["orgaos"][self.orgao].remove(cat_val)
            return data
        
        await update_categories_safe(remove)
        await interaction.response.edit_message(
            content=f"✅ Categoria **{cat_val}** removida de {self.orgao}!",
            view=PainelGerenciamento(self.bot)
        )

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot),
            view=PainelGerenciamento(self.bot)
        )


class ExcluirCategoriaStep1View(BaseSelectionView):
    """View para exclusão de categoria (passo 1)"""
    
    def __init__(self, bot):
        data = get_categories()
        orgaos = sorted(list(data.get("orgaos", {}).keys()))
        super().__init__(bot, orgaos, "1️⃣ De qual Orgão é a categoria?", "del_cat_step1")

    async def on_select(self, interaction: discord.Interaction):
        orgao = interaction.data['values'][0]
        await interaction.response.edit_message(
            content=f"📂 Selecionado: **{orgao}**. Agora escolha a categoria para apagar:",
            view=ExcluirCategoriaStep2View(self.bot, orgao)
        )

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot),
            view=PainelGerenciamento(self.bot)
        )

class PainelGerenciamento(BaseView):
    """Painel de gerenciamento de categorias, órgãos e equipes"""

    @ui.button(label="Excluir Orgão", style=discord.ButtonStyle.danger, row=0, emoji="🏢")
    async def btn_del_orgao(self, interaction: discord.Interaction, button: ui.Button):
        view = ExcluirOrgaoView(self.bot)
        if len(view.children) < 2:
            await interaction.response.send_message("⚠️ Não há orgãos para excluir.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="🗑️ **Modo de Exclusão:** Selecione o Orgão para apagar permanentemente.",
            view=view,
            embed=None
        )

    @ui.button(label="Excluir Categoria", style=discord.ButtonStyle.danger, row=0, emoji="📂")
    async def btn_del_cat(self, interaction: discord.Interaction, button: ui.Button):
        view = ExcluirCategoriaStep1View(self.bot)
        if len(view.children) < 2:
            await interaction.response.send_message("⚠️ Não há dados para excluir.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="🗑️ **Modo de Exclusão:** Primeiro, escolha o Orgão.",
            view=view,
            embed=None
        )

    @ui.button(label="Excluir Equipe", style=discord.ButtonStyle.danger, row=0, emoji="🛠️")
    async def btn_del_equipe(self, interaction: discord.Interaction, button: ui.Button):
        view = ExcluirEquipeView(self.bot)
        if len(view.children) < 2:
            await interaction.response.send_message("⚠️ Não há equipes para excluir.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="🗑️ **Modo de Exclusão:** Selecione a Equipe para apagar.",
            view=view,
            embed=None
        )

    @ui.button(label="Voltar", style=discord.ButtonStyle.secondary, row=1, emoji="↩️")
    async def btn_back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot),
            view=PainelPrincipal(self.bot)
        )

# --- UI: PERMISSÕES ---
class PainelPermissoes(BaseView):
    """Painel de gerenciamento de permissões por cargo"""
    
    PAGE_INFO = {
        1: {
            "title": "🛡️ Permissões de Cargos - Página 1/2",
            "keys": ["extracao_canal", "extracao_tudo"],
            "labels": ["📦 Extração Canal", "🌎 Extração Global"]
        },
        2: {
            "title": "🛡️ Permissões de Cargos - Página 2/2",
            "keys": ["resolvido", "reabrir"],
            "labels": ["✅ Finalizar Chamado", "🔓 Reabrir Chamado"]
        }
    }

    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.page = 1
        self.temp_perms = get_config()["perms"].copy()
        self.update_components()

    def get_defaults(self, key: str) -> list:
        """Retorna valores padrão para seletor"""
        ids = self.temp_perms.get(key, [])
        return [discord.Object(id=i) for i in ids]

    def build_status_embed(self) -> discord.Embed:
        """Constrói embed de status"""
        info = self.PAGE_INFO[self.page]
        embed = discord.Embed(title=info["title"], description="Defina quem acessa cada função.", color=0x3498db)
        
        for key, label in zip(info["keys"], info["labels"]):
            ids = self.temp_perms.get(key, [])
            valor = ", ".join([f"<@&{i}>" for i in ids]) if ids else "❌ *Ninguém*"
            embed.add_field(name=label, value=valor, inline=False)
        
        embed.set_footer(text=f"Página {self.page}/2")
        return embed

    def update_components(self):
        """Reconstrói componentes interativos"""
        self.clear_items()
        info = self.PAGE_INFO[self.page]
        
        # Adiciona seletores de cargo
        for key, label in zip(info["keys"], info["labels"]):
            select = ui.RoleSelect(
                placeholder=label,
                min_values=0,
                max_values=20,
                default_values=self.get_defaults(key)
            )
            select.callback = self._create_callback(key)
            self.add_item(select)
        
        # Navegação
        if self.page > 1:
            btn_prev = ui.Button(label="⬅️ Anterior", style=discord.ButtonStyle.secondary)
            btn_prev.callback = self._on_prev
            self.add_item(btn_prev)
        
        if self.page < 2:
            btn_next = ui.Button(label="Próximo ➡️", style=discord.ButtonStyle.secondary)
            btn_next.callback = self._on_next
            self.add_item(btn_next)
        
        btn_home = ui.Button(label="🏠 Voltar", style=discord.ButtonStyle.secondary)
        btn_home.callback = self._on_home
        self.add_item(btn_home)
        
        btn_save = ui.Button(label="💾 Salvar", style=discord.ButtonStyle.success)
        btn_save.callback = self._on_save
        self.add_item(btn_save)

    def _create_callback(self, key: str):
        """Factory para callbacks de seleção"""
        async def callback(inter: discord.Interaction):
            self.temp_perms[key] = [int(x) for x in inter.data['values']]
            await inter.response.edit_message(embed=self.build_status_embed(), view=self)
        return callback

    async def _on_next(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(embed=self.build_status_embed(), view=self)

    async def _on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(embed=self.build_status_embed(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        async with DataManager.get_lock("config"):
            cfg = get_config()
            cfg["perms"] = self.temp_perms
            await DataManager.save_async(DATA_FILES["config"], cfg)
        
        await interaction.response.edit_message(
            content="✅ **Permissões Salvas!**",
            embed=build_dashboard_embed(self.bot),
            view=PainelPrincipal(self.bot)
        )

    async def _on_home(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_dashboard_embed(self.bot),
            view=PainelPrincipal(self.bot)
        )

# --- UI: TELA DE CANAIS ---
class SeletorCanaisView(BaseView):
    """Seletor de canais para monitoramento"""
    
    def __init__(self, current_connected_ids: list, bot_instance):
        super().__init__(bot_instance)
        self.selected_ids = current_connected_ids
        
        # Seletor de canais
        defaults = [discord.Object(id=int(i)) for i in self.selected_ids]
        select = ui.ChannelSelect(
            placeholder="Selecione os canais...",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=25,
            default_values=defaults
        )
        select.callback = self._on_select_channels
        self.add_item(select)
        
        # Botão voltar
        btn = ui.Button(label="🏠 Voltar / Cancelar", style=discord.ButtonStyle.secondary, row=1)
        btn.callback = self._on_home
        self.add_item(btn)

    async def _on_home(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot),
            view=PainelPrincipal(self.bot)
        )

    async def _on_select_channels(self, interaction: discord.Interaction):
        """Atualiza canais selecionados"""
        novos = [str(x) for x in interaction.data['values']]
        
        async with DataManager.get_lock("config"):
            cfg = get_config()
            antigos = cfg.get("connected_channels", {})
            novo_dict = {}
            
            for cid in novos:
                novo_dict[cid] = antigos[cid] if cid in antigos else {
                    "last_marker_timestamp": datetime.min.replace(tzinfo=BRT_OFFSET).isoformat()
                }
            
            cfg["connected_channels"] = novo_dict
            await DataManager.save_async(DATA_FILES["config"], cfg)
        
        await interaction.response.edit_message(
            content="✅ **Canais Atualizados!**",
            embed=build_dashboard_embed(self.bot),
            view=PainelPrincipal(self.bot)
        )

# --- UI: PAINEL PRINCIPAL (JANELA) ---
class PainelPrincipal(BaseView):
    """Painel principal de controle do bot"""
    
    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.last_backup_click = 0

    @ui.button(label="Canais", style=discord.ButtonStyle.secondary, row=0, emoji="📡")
    async def btn_canais(self, interaction: discord.Interaction, button: ui.Button):
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Use o painel fora de tópicos.", ephemeral=True)
            return
        
        cfg = get_config()
        ids = list(cfg["connected_channels"].keys())
        embed = discord.Embed(
            title="📡 Gerenciar Canais",
            description="Selecione no menu abaixo quais canais monitorar.",
            color=0x2ecc71
        )
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=SeletorCanaisView(ids, self.bot)
        )

    @ui.button(label="Permissões", style=discord.ButtonStyle.secondary, row=0, emoji="🛡️")
    async def btn_perms(self, interaction: discord.Interaction, button: ui.Button):
        view = PainelPermissoes(self.bot)
        await interaction.response.edit_message(
            content=None,
            embed=view.build_status_embed(),
            view=view
        )

    @ui.button(label="Configurações", style=discord.ButtonStyle.secondary, row=0, emoji="⚙️")
    async def btn_config(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot),
            view=PainelGerenciamento(self.bot)
        )

    @ui.button(label="Forçar Backup", style=discord.ButtonStyle.primary, row=1, emoji="💾")
    async def btn_backup(self, interaction: discord.Interaction, button: ui.Button):
        now = datetime.now().timestamp()
        
        # Controla frequência
        if now - self.last_backup_click < 300:
            restante = int(300 - (now - self.last_backup_click))
            await interaction.response.send_message(
                f"⏳ **Aguarde {restante}s** para novo backup.",
                ephemeral=True
            )
            return
        
        self.last_backup_click = now
        await interaction.response.defer(ephemeral=True)
        
        try:
            stats, zip_p = await executar_com_retry(perform_extraction, None, force_all=False)
            msg = f"✅ **Backup Manual!** Novos: {stats['topicos']}"
            
            if zip_p:
                await interaction.followup.send(msg, file=discord.File(zip_p), ephemeral=True)
            else:
                await interaction.followup.send(msg + "\n(Sem arquivos novos)", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Erro no backup manual: {e}", ephemeral=True)

    @ui.button(label="Sair", style=discord.ButtonStyle.danger, row=1, emoji="✖️")
    async def btn_close(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            content="👋 Sessão finalizada.",
            embed=None,
            view=None
        )

def build_dashboard_embed(bot_inst: commands.Bot) -> discord.Embed:
    """Constrói o embed do painel de controle"""
    cfg = get_config()
    con_channels = cfg.get("connected_channels", {})
    count = len(con_channels)
    
    # Formata lista de canais
    lista = []
    for cid in con_channels:
        ch = bot_inst.get_channel(int(cid))
        lista.append(f"• {ch.name}" if ch else f"• ID {cid}")
    
    txt_canais = "\n".join(lista[:5])
    if count > 5:
        txt_canais += f"\n... e mais {count-5}"
    if not txt_canais:
        txt_canais = "Nenhum canal configurado."
    
    # Calcula próximo backup
    next_run = datetime.now(BRT_OFFSET).replace(hour=HORA_BACKUP, minute=MINUTO_BACKUP, second=0)
    if datetime.now(BRT_OFFSET) > next_run:
        next_run += timedelta(days=1)
    ts_next = int(next_run.timestamp())
    
    # Constrói embed
    embed = discord.Embed(
        title="🎛️  Painel de Controle",
        description="Gerenciamento e Status do Bot de Suporte",
        color=0x5865F2
    )
    
    # Status do bot
    ping = int(bot_inst.latency * 1000)
    status_emoji = "🟢" if ping < 200 else "🟡" if ping < 500 else "🔴"
    embed.add_field(
        name=f"{status_emoji} Status",
        value=f"**Ping:** `{ping}ms`\n**Online:** Sim",
        inline=True
    )
    
    embed.add_field(
        name="⏳ Próximo Backup",
        value=f"<t:{ts_next}:R>\n(`{HORA_BACKUP:02d}:{MINUTO_BACKUP:02d}`)",
        inline=True
    )
    
    embed.add_field(name="", value="⠀", inline=False)
    embed.add_field(
        name=f"📡 Canais Monitorados: {count}",
        value=f"```text\n{txt_canais}\n```",
        inline=False
    )
    
    embed.set_thumbnail(url=bot_inst.user.avatar.url if bot_inst.user.avatar else None)
    embed.set_footer(text=f"Última atualização: {datetime.now(BRT_OFFSET).strftime('%H:%M:%S')}")
    
    return embed

# --- MODAIS DE CRIAÇÃO ---
class NovaEquipeModal(ui.Modal, title="Nova Equipe de Resolução"):
    nome = ui.TextInput(label="Nome da Equipe", placeholder="Ex: QA, Design, Infra...", max_length=50) 
    async def on_submit(self, interaction: discord.Interaction):
        nome_equipe = sanitize_input(self.nome.value) 
        def add(data):
            if nome_equipe and nome_equipe not in data["equipes"]:
                data["equipes"].append(nome_equipe)
                data["equipes"].sort()
            return data
        await update_categories_safe(add)
        view = PainelResolucao()
        view.selections = self.view_origin.selections
        view.selections["quem_tratou"] = nome_equipe
        await view.finalizar_processo(interaction)

class NovoOrgaoModal(ui.Modal, title="Criar Novo Orgão"):
    nome = ui.TextInput(label="Nome do Orgão", placeholder="Ex: Financeiro, RH...", max_length=50)
    async def on_submit(self, interaction: discord.Interaction):
        novo_orgao = sanitize_input(self.nome.value)
        def add(data):
            if novo_orgao and novo_orgao not in data["orgaos"]: data["orgaos"][novo_orgao] = [] 
            return data
        await update_categories_safe(add)
        view = PainelResolucao()
        view.selections["orgao"] = novo_orgao
        await view.add_categoria_select(interaction)

class NovaCategoriaModal(ui.Modal, title="Criar Nova Categoria"):
    nome = ui.TextInput(label="Nome da Categoria", placeholder="Ex: Erro 404...", max_length=50)
    async def on_submit(self, interaction: discord.Interaction):
        nova_cat = sanitize_input(self.nome.value)
        orgao_atual = self.view_origin.selections["orgao"]
        def add(data):
            if orgao_atual in data["orgaos"]:
                if nova_cat not in data["orgaos"][orgao_atual]:
                    data["orgaos"][orgao_atual].append(nova_cat)
                    data["orgaos"][orgao_atual].sort()
            return data
        await update_categories_safe(add)
        view = PainelResolucao()
        view.selections = self.view_origin.selections
        view.selections["categoria"] = nova_cat
        await view.add_equipe_select(interaction)

# --- UI DE RESOLUÇÃO (WIZARD) ---
class PainelResolucao(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.selections = {"orgao": None, "categoria": None, "quem_tratou": None}
        self.add_orgao_select()

    def add_orgao_select(self):
        self.clear_items()
        data = get_categories()
        orgaos_list = list(data.get("orgaos", {}).keys())
        orgaos_list.sort()
        if orgaos_list:
            options = [discord.SelectOption(label=o, value=o) for o in orgaos_list[:25]]
            select = ui.Select(placeholder="1️⃣ Selecione o Orgão...", options=options, custom_id="sel_orgao", row=0)
            select.callback = self.callback_orgao
            self.add_item(select)
        btn_novo = ui.Button(label="➕ Criar Orgão", style=discord.ButtonStyle.success, row=1)
        btn_novo.callback = self.callback_novo_orgao
        self.add_item(btn_novo)
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.danger, row=1, emoji="✖️")
        btn_cancel.callback = self.callback_cancelar
        self.add_item(btn_cancel)

    async def callback_orgao(self, interaction: discord.Interaction):
        escolha = interaction.data['values'][0]
        self.selections["orgao"] = escolha
        await self.add_categoria_select(interaction)

    async def callback_novo_orgao(self, interaction: discord.Interaction):
        modal = NovoOrgaoModal()
        modal.view_origin = self
        await interaction.response.send_modal(modal)

    async def callback_cancelar(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="🚫 **Ação cancelada.**", view=None, embed=None)

    async def add_categoria_select(self, interaction: discord.Interaction):
        self.clear_items()
        orgao_escolhido = self.selections["orgao"]
        data = get_categories()
        cats_list = data.get("orgaos", {}).get(orgao_escolhido, [])
        cats_list.sort() 
        if cats_list:
            options = [discord.SelectOption(label=c, value=c) for c in cats_list[:25]]
            select = ui.Select(placeholder=f"2️⃣ Categoria de {orgao_escolhido}...", options=options, custom_id="sel_cat", row=0)
            select.callback = self.callback_categoria
            self.add_item(select)
        btn_nova_cat = ui.Button(label="➕ Criar Categoria", style=discord.ButtonStyle.success, row=1)
        btn_nova_cat.callback = self.callback_nova_categoria
        self.add_item(btn_nova_cat)
        btn_back = ui.Button(label="Voltar", style=discord.ButtonStyle.secondary, emoji="⬅️", row=2)
        btn_back.callback = self.callback_voltar_orgao
        self.add_item(btn_back)
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.danger, row=2, emoji="✖️")
        btn_cancel.callback = self.callback_cancelar
        self.add_item(btn_cancel)
        txt_msg = f"🏢 Orgão Selecionado: **{orgao_escolhido}**\nAgora escolha (ou crie) a categoria:"
        if interaction.response.is_done(): await interaction.edit_original_response(content=txt_msg, view=self)
        else: await interaction.response.edit_message(content=txt_msg, view=self)

    async def callback_voltar_orgao(self, interaction: discord.Interaction):
        self.add_orgao_select()
        await interaction.response.edit_message(content="📁 **Finalizar Chamado (Reinício):**", view=self)

    async def callback_categoria(self, interaction: discord.Interaction):
        escolha = interaction.data['values'][0]
        self.selections["categoria"] = escolha
        await self.add_equipe_select(interaction)

    async def callback_nova_categoria(self, interaction: discord.Interaction):
        modal = NovaCategoriaModal()
        modal.view_origin = self
        await interaction.response.send_modal(modal)

    async def add_equipe_select(self, interaction: discord.Interaction):
        self.clear_items()
        data = get_categories()
        equipes = data.get("equipes", [])
        if equipes:
            options = [discord.SelectOption(label=e, value=e) for e in equipes[:25]]
            select = ui.Select(placeholder="3️⃣ Qual equipe resolveu?", options=options, custom_id="sel_equipe", row=0)
            select.callback = self.callback_equipe
            self.add_item(select)
        btn_nova = ui.Button(label="➕ Criar Equipe", style=discord.ButtonStyle.primary, row=1)
        btn_nova.callback = self.callback_nova_equipe
        self.add_item(btn_nova)
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.danger, row=1, emoji="✖️")
        btn_cancel.callback = self.callback_cancelar
        self.add_item(btn_cancel)
        txt = f"🏢 Orgão: **{self.selections['orgao']}**\n📂 Categoria: **{self.selections['categoria']}**\n\nAgora selecione a equipe responsável:"
        if interaction.response.is_done(): await interaction.edit_original_response(content=txt, view=self)
        else: await interaction.response.edit_message(content=txt, view=self)

    async def callback_equipe(self, interaction: discord.Interaction):
        equipe = interaction.data['values'][0]
        self.selections["quem_tratou"] = equipe
        await self.finalizar_processo(interaction)

    async def callback_nova_equipe(self, interaction: discord.Interaction):
        modal = NovaEquipeModal()
        modal.view_origin = self 
        await interaction.response.send_modal(modal)

    async def finalizar_processo(self, interaction: discord.Interaction):
        orgao = self.selections["orgao"]
        cat = self.selections["categoria"]
        quem = self.selections["quem_tratou"]
        await finalizar_topico_completo(interaction, orgao, cat, quem)

async def finalizar_topico_completo(interaction: discord.Interaction, orgao: str, categoria: str, equipe_nome: str) -> None:
    """Finaliza um tópico com informações de resolução"""
    thread = interaction.channel
    
    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message("❌ Use dentro de um tópico.", ephemeral=True)
        return
    
    # Limpa mensagens antigas
    await apagar_mensagens_antigas_bot(thread, "Tópico Reaberto!")
    await apagar_mensagens_antigas_bot(thread, "Chamado Finalizado!")
    
    # Registra resolução
    await log_resolution_safe(thread.id, thread.name, equipe_nome, interaction.user.id, categoria, orgao)
    await registrar_log_safe("RESOLVIDO", interaction.user.name, f"Tópico: {thread.name} | Orgão: {orgao} | Cat: {categoria}")
    
    # Cria embed de sucesso
    embed = discord.Embed(title="✅ Chamado Finalizado!", color=0x2ecc71)
    embed.add_field(name="🏢 Orgão", value=f"**{orgao}**", inline=True)
    embed.add_field(name="📂 Categoria", value=f"`{categoria}`", inline=True)
    embed.add_field(name="🛠️ Quem tratou", value=f"**{equipe_nome}**", inline=False)
    embed.set_footer(text=f"Fechado por {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    embed.timestamp = datetime.now()
    
    # Responde interaction
    if interaction.response.is_done():
        await interaction.edit_original_response(content=None, embed=embed, view=None)
    else:
        await interaction.response.edit_message(content=None, embed=embed, view=None)
    
    # Tranca tópico
    try:
        await thread.edit(locked=True, archived=True, reason=f"{orgao} - {categoria} ({equipe_nome})")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Erro ao trancar: {e}", ephemeral=True)

# --- MOTOR DE EXTRAÇÃO ---

class ExtractionEngine:
    """Motor centralizado de extração de tópicos"""
    
    @staticmethod
    def limpar_nome(nome: str) -> str:
        """Limpa nome para uso em paths"""
        return clean_name(nome)
    
    @staticmethod
    def processar_linha_toon(item: dict, colunas: list, nome_pasta_anexos=None) -> list:
        """Processa linha de mensagem para formato TOON"""
        row_values = []
        texto = str(item.get("conteudo", "")).strip()
        lista_anexos = item.get("anexos", [])
        
        tags = []
        for cam in lista_anexos:
            nome = os.path.basename(cam)
            if "_" in nome:
                parts = nome.split("_", 1)
                nome = parts[1] if len(parts) > 1 and parts[0].isdigit() else nome
            
            tag = f"[ANEXO: {nome_pasta_anexos}/{nome}]" if nome_pasta_anexos else f"[ANEXO: {nome}]"
            tags.append(tag)
        
        str_anexos = " ".join(tags)
        conteudo = f"{texto} {str_anexos}".strip() if texto else str_anexos
        
        for col in colunas:
            if col == "mensagem":
                val = conteudo
            elif col == "data":
                val = item.get("timestamp_brt", "")[:19].replace("T", " ")
            else:  # autor
                val = item.get("autor", {}).get("nome", "Desconhecido")
            
            row_values.append(str(val).replace("\n", " ").strip())
        
        return row_values
    
    @staticmethod
    def gerar_texto_toon(contexto: dict, mensagens: list, pasta_ref: str) -> str:
        """Gera texto em formato TOON"""
        lines = ["contexto:"] + [f"  {k}: {v}" for k, v in contexto.items()]
        
        if mensagens:
            header = "data,autor,mensagem"
            lines.append(f"mensagens[{len(mensagens)}]{{{header}}}:")
            lines.extend([
                f"  {', '.join(ExtractionEngine.processar_linha_toon(m, ['data', 'autor', 'mensagem'], pasta_ref))}"
                for m in mensagens
            ])
        
        return "\n".join(lines)
    
    @staticmethod
    async def extrair_topico_arquivado(session: aiohttp.ClientSession, thread: discord.Thread, pasta_destino: str) -> bool:
        """Extrai um tópico arquivado"""
        nome = ExtractionEngine.limpar_nome(thread.name)
        pasta_anexos = os.path.join(pasta_destino, f"anexos_{nome}")
        msgs = []
        tem_anexos = False
        
        # Busca informações de resolução
        db = DataManager.load_json(DATA_FILES["db"], [])
        db_entry = next((r for r in db if r["thread_id"] == str(thread.id)), None)
        cat = db_entry["categoria"] if db_entry else "Não Categorizado"
        orgao_val = db_entry.get("orgao", "N/A") if db_entry else "N/A"
        
        try:
            # Coleta mensagens
            async for m in thread.history(limit=None, oldest_first=True):
                # Ignora mensagem de conclusão do bot
                if m.author.id == bot.user.id and "Chamado Finalizado!" in m.content:
                    continue
                
                paths = []
                
                # Baixa anexos
                if m.attachments:
                    tem_anexos = True
                    if not os.path.exists(pasta_anexos):
                        os.makedirs(pasta_anexos, exist_ok=True)
                    
                    for a in m.attachments:
                        p = os.path.join(pasta_anexos, f"{a.id}_{a.filename}")
                        if not os.path.exists(p):
                            try:
                                async with session.get(a.url) as r:
                                    if r.status == 200:
                                        with open(p, 'wb') as f:
                                            f.write(await r.read())
                            except:
                                pass
                        paths.append(p)
                
                msgs.append({
                    "timestamp_brt": m.created_at.astimezone(BRT_OFFSET).isoformat(),
                    "autor": {"nome": m.author.name},
                    "conteudo": m.content,
                    "anexos": paths
                })
        
        except discord.Forbidden:
            return False
        
        # Salva extração
        if msgs:
            nome_canal_origem = thread.parent.name if thread.parent else "Desconhecido"
            ts_arquivado = str(thread.archive_timestamp.astimezone(BRT_OFFSET)) if thread.archive_timestamp else "EM_ABERTO"
            ctx = {
                "origem": nome_canal_origem,
                "nome": thread.name,
                "orgao": orgao_val,
                "categoria": cat,
                "id": str(thread.id),
                "arquivado_em": ts_arquivado
            }
            
            with open(os.path.join(pasta_destino, f"topico_{nome}.txt"), "w", encoding="utf-8") as f:
                f.write(ExtractionEngine.gerar_texto_toon(ctx, msgs, f"anexos_{nome}" if tem_anexos else None))
            
            # Remove pasta de anexos se vazia
            if os.path.exists(pasta_anexos) and not os.listdir(pasta_anexos):
                os.rmdir(pasta_anexos)
            
            return True
        
        return False

async def perform_extraction(target_channels=None, force_all: bool = False) -> tuple:
    """Extrai tópicos arquivados dos canais especificados"""
    async with DataManager.get_lock("config"):
        cfg = get_config()
    
    ts_now = datetime.now(BRT_OFFSET)
    raiz = f"./extracoes/extracao_arquivados_{ts_now.strftime('%d_%m_%Y_%H_%M')}"
    stats = {"canais": 0, "topicos": 0}
    extracted = False
    
    # Define canais a processar
    target = target_channels if target_channels else [
        bot.get_channel(int(cid)) for cid in cfg["connected_channels"]
        if bot.get_channel(int(cid))
    ]
    
    async with aiohttp.ClientSession() as session:
        for ch in target:
            if not ch:
                continue
            
            # Obtém último timestamp processado
            last_ts_str = cfg["connected_channels"].get(str(ch.id), {}).get("last_marker_timestamp")
            last_ts = None
            
            if not force_all and str(ch.id) in cfg["connected_channels"] and last_ts_str:
                last_ts = datetime.fromisoformat(last_ts_str)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=BRT_OFFSET)
            
            pasta = os.path.join(raiz, f"chat_{clean_name(ch.name)}")
            
            # Coleta tópicos arquivados
            threads = [t async for t in ch.archived_threads(limit=None)]
            try:
                threads.extend([t async for t in ch.archived_threads(private=True, limit=None)])
            except:
                pass
            
            # Processa cada tópico
            cnt = 0
            for t in threads:
                if not t.archive_timestamp:
                    continue
                if last_ts and t.archive_timestamp <= last_ts:
                    continue
                
                if not os.path.exists(pasta):
                    os.makedirs(pasta, exist_ok=True)
                
                if await ExtractionEngine.extrair_topico_arquivado(session, t, pasta):
                    cnt += 1
                    extracted = True
            
            # Atualiza timestamp se houver novos tópicos
            if cnt > 0:
                stats["canais"] += 1
                stats["topicos"] += cnt
                
                async with DataManager.get_lock("config"):
                    cfg = get_config()
                    if not force_all and str(ch.id) in cfg["connected_channels"]:
                        cfg["connected_channels"][str(ch.id)]["last_marker_timestamp"] = ts_now.isoformat()
                        await DataManager.save_async(DATA_FILES["config"], cfg)
    
    # Compacta resultado
    zip_path = ""
    if extracted:
        try:
            loop = asyncio.get_running_loop()
            zip_path = await loop.run_in_executor(None, shutil.make_archive, raiz, 'zip', raiz)
            
            # Remove pasta descompactada
            if os.path.exists(raiz):
                await loop.run_in_executor(None, shutil.rmtree, raiz)
        except Exception as e:
            print(f"Erro ao zipar: {e}")
            raise e
    elif os.path.exists(raiz):
        shutil.rmtree(raiz)
    
    return stats, zip_path

@bot.event
async def on_message(message):
    if message.author.id == bot.user.id: return
    if isinstance(message.channel, discord.Thread) and message.channel.locked:
        try:
            await message.delete()
            msg = await message.channel.send(f"⛔ {message.author.mention}, este tópico está finalizado! Use **/reabrir** para voltar a interagir.")
            await asyncio.sleep(5)
            await msg.delete()
        except: pass

# --- TRATAMENTO GLOBAL DE ERROS ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Se o erro for uma falha de permissão (check_permission retornou False)
    if isinstance(error, app_commands.CheckFailure):
        # A mensagem de "Sem permissão" geralmente já é enviada dentro do check_permission.
        # Mas se por acaso não foi (interaction não respondida), enviamos aqui para garantir.
        if not interaction.response.is_done():
            await interaction.response.send_message("⛔ Você não tem permissão para executar este comando.", ephemeral=True)
        # Não fazemos nada mais (print/log) porque é um comportamento esperado.
    else:
        # Se for outro erro, printamos no console para debug
        print(f"❌ Erro não tratado no comando: {error}")
        traceback.print_exc()

# --- COMANDOS ---

@bot.tree.command(name="iniciar", description="[ADMIN] Configuração Inicial do Bot (Setup).")
@app_commands.default_permissions(administrator=True)
async def iniciar(interaction: discord.Interaction):
    """
    Comando para configuração inicial dos IDs.
    Só pode ser usado por quem tem permissão de Administrador no servidor.
    """
    embed = discord.Embed(
        title="🛠️ Painel de Setup Inicial",
        description="Configure os canais e o cargo de administrador mestre para que o bot funcione corretamente.",
        color=0xFEE75C
    )
    embed.add_field(name="Instruções", value="1. Selecione o cargo de Admin.\n2. Selecione o canal de comandos.\n3. Selecione o canal de cronômetro.\n4. Clique em Salvar.")
    await interaction.response.send_message(embed=embed, view=PainelSetup(bot), ephemeral=True)

@bot.tree.command(name="painel", description="[MASTER] Abre o Sistema de Controle.")
@app_commands.checks.cooldown(1, 10.0)
@is_master()
async def painel(interaction: discord.Interaction):
    """Abre o painel principal de controle"""
    cmd_channel_id = get_setup_id("id_canal_comandos")
    
    if cmd_channel_id and interaction.channel_id != cmd_channel_id:
        await interaction.response.send_message(f"❌ Este comando só pode ser usado no canal <#{cmd_channel_id}>.", ephemeral=True)
        return
    
    if isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("❌ Este comando não pode ser usado dentro de um tópico.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send("🔄 **Inicializando sistema...**", ephemeral=True)
    await asyncio.sleep(1)
    
    embed = build_dashboard_embed(bot)
    await interaction.edit_original_response(content=None, embed=embed, view=PainelPrincipal(bot))


@bot.tree.command(name="extracao_canal", description="[EXTRACAO] Extrai tópicos deste canal.")
@app_commands.checks.cooldown(1, 60.0)
@check_permission("extracao_canal")
@app_commands.checks.check(check_valid_channel())
async def extracao_canal(interaction: discord.Interaction, canal: discord.TextChannel):
    """Extrai tópicos de um canal específico"""
    await interaction.response.defer(ephemeral=False)
    await registrar_log_safe("EXTRACAO_MANUAL", interaction.user.name, f"Canal: {canal.name}")
    
    stats, zip_p = await perform_extraction([canal], force_all=False)
    msg = f"📦 **Extração Canal**\nNovos Tópicos: {stats['topicos']}"
    
    if zip_p:
        await interaction.followup.send(msg, file=discord.File(zip_p))
    else:
        await interaction.followup.send(msg + "\n(Nada novo)")


@bot.tree.command(name="extracao_tudo", description="[EXTRACAO] Extrai tópicos de todos os canais.")
@app_commands.checks.cooldown(1, 30.0)
@check_permission("extracao_tudo")
@app_commands.checks.check(check_valid_channel())
async def extracao_tudo(interaction: discord.Interaction):
    """Extrai tópicos de todos os canais configurados"""
    await interaction.response.defer(ephemeral=False)
    await registrar_log_safe("EXTRACAO_MANUAL", interaction.user.name, "GLOBAL")
    
    stats, zip_p = await perform_extraction(None, force_all=False)
    msg = f"📦 **Extração Global**\nCanais: {stats['canais']} | Tópicos: {stats['topicos']}"
    
    if zip_p:
        await interaction.followup.send(msg, file=discord.File(zip_p))
    else:
        await interaction.followup.send(msg + "\n(Nada novo)")

@bot.tree.command(name="resolvido", description="[SUPORTE] Finaliza chamado.")
@check_permission("resolvido")
async def resolvido(interaction: discord.Interaction):
    """Marca um tópico como resolvido"""
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("❌ Use num tópico.", ephemeral=True)
        return
    
    if interaction.channel.locked:
        await interaction.response.send_message(
            "❌ Este tópico já está resolvido/trancado. Use `/reabrir` se necessário.",
            ephemeral=True
        )
        return
    
    get_categories()
    await interaction.response.send_message("📁 **Finalizar Chamado:**", view=PainelResolucao())


@bot.tree.command(name="reabrir", description="[SUPORTE] Reabre o tópico.")
@check_permission("reabrir")
async def reabrir(interaction: discord.Interaction):
    """Reabre um tópico trancado"""
    thread = interaction.channel
    
    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message("❌ Use dentro de um tópico.", ephemeral=True)
        return
    
    if not thread.locked and not thread.archived:
        await interaction.response.send_message(
            "❌ Este tópico já está aberto. Use `/resolvido` para finalizar.",
            ephemeral=True
        )
        return
    
    # Limpa mensagens antigas
    await apagar_mensagens_antigas_bot(thread, "Chamado Finalizado!")
    foi_removido = remove_resolution(thread.id)
    
    try:
        await thread.edit(locked=False, archived=False, reason=f"Reaberto por {interaction.user.name}")
        await registrar_log_safe("REABRIR", interaction.user.name, f"Tópico: {thread.name}")
        
        msg = "🔓 **Tópico Reaberto!**"
        if not foi_removido:
            msg += "\n(Nota: Não constava no banco)."
        
        await interaction.response.send_message(msg)
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Erro: {e}", ephemeral=True)

# --- LOOPS DE BACKGROUND ---

@tasks.loop(time=time(hour=HORA_BACKUP, minute=MINUTO_BACKUP, tzinfo=BRT_OFFSET))
async def daily_extraction_loop():
    """Executa extração automática diária"""
    print("Extração auto iniciada...")
    await registrar_log_safe("AUTO_EXTRACAO", "SISTEMA", "Início")
    
    try:
        stats, zip_path = await executar_com_retry(perform_extraction, None, force_all=False)
        channel_id = get_setup_id("id_canal_comandos")
        
        if channel_id:
            ch = bot.get_channel(channel_id)
            if ch:
                if zip_path:
                    await ch.send(
                        f"🤖 **Backup Auto!**\nNovos: {stats['topicos']}",
                        file=discord.File(zip_path)
                    )
                    await enviar_log_discord("✅ Backup Auto Sucesso", "Finalizado.", 0x2ecc71)
                else:
                    await ch.send("🤖 **Backup Auto:** Nada novo hoje.")
    except Exception as e:
        print(f"Erro fatal loop: {e}")


@daily_extraction_loop.before_loop
async def before_daily():
    """Aguarda bot estar pronto antes de iniciar loop"""
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def update_countdown_loop():
    """Atualiza mensagem de countdown para próximo backup"""
    global countdown_message_id
    
    channel_id = get_setup_id("id_canal_countdown")
    if not channel_id:
        return
    
    ch = bot.get_channel(channel_id)
    if not ch:
        return
    
    # Calcula próximo backup
    now = datetime.now(BRT_OFFSET)
    target = now.replace(hour=HORA_BACKUP, minute=MINUTO_BACKUP, second=0)
    if now >= target:
        target += timedelta(days=1)
    
    ts = int(target.timestamp())
    txt = f"⏳ Próximo backup automático: <t:{ts}:R>"
    
    try:
        if not countdown_message_id:
            # Remove mensagens antigas do bot
            async for m in ch.history(limit=5):
                if m.author == bot.user:
                    await m.delete()
            
            # Envia nova mensagem
            m = await ch.send(txt)
            countdown_message_id = m.id
        else:
            # Atualiza mensagem existente
            try:
                msg = await ch.fetch_message(countdown_message_id)
                if msg.content != txt:
                    await msg.edit(content=txt)
            except discord.NotFound:
                countdown_message_id = None
    except:
        countdown_message_id = None


@update_countdown_loop.before_loop
async def before_count():
    """Aguarda bot estar pronto antes de iniciar loop"""
    await bot.wait_until_ready()


# --- EVENTOS DO BOT ---

@bot.event
async def on_message(message: discord.Message):
    """Impede mensagens em tópicos trancados"""
    if message.author.id == bot.user.id:
        return
    
    if isinstance(message.channel, discord.Thread) and message.channel.locked:
        try:
            await message.delete()
            msg = await message.channel.send(
                f"⛔ {message.author.mention}, este tópico está finalizado! Use **/reabrir** para voltar a interagir."
            )
            await asyncio.sleep(5)
            await msg.delete()
        except:
            pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Trata erros globais de comandos"""
    if isinstance(error, app_commands.CheckFailure):
        # Erro de permissão já foi tratado dentro do check
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "⛔ Você não tem permissão para executar este comando.",
                ephemeral=True
            )
    else:
        # Outros erros são logados
        print(f"❌ Erro não tratado no comando: {error}")
        traceback.print_exc()


@bot.event
async def on_ready():
    """Inicialização do bot"""
    print(f'Bot Online: {bot.user}')
    await registrar_log_safe("STARTUP", "SISTEMA", f"Bot Online: {bot.user}")
    
    # Garante estrutura de categorias
    get_categories()
    
    # Sincroniza comandos
    await bot.tree.sync()
    
    # Inicia loops se ainda não estiverem rodando
    if not daily_extraction_loop.is_running():
        daily_extraction_loop.start()
    
    if not update_countdown_loop.is_running():
        update_countdown_loop.start()


# --- INICIALIZAÇÃO DO BOT ---
if __name__ == "__main__":
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("\n❌ ERRO CRÍTICO: Token não encontrado!")
    else:
        bot.run(token)