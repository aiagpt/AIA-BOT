"""
ui_components.py - Interface completa adaptada para Multi-Server
Restaura todas as funcionalidades originais (Permissões, Gerenciamento, Backup) com isolamento de dados.
"""
import discord
from discord import ui
from datetime import datetime, timedelta
from config import (
    DataManager, get_config, get_categories, get_setup_id, 
    sanitize_input, update_categories, update_config,
    BRT_OFFSET, HORA_BACKUP, MINUTO_BACKUP
)

# --- CLASSES BASE ---
class BaseView(ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance

class BaseSelectionView(BaseView):
    """View genérica para menus de seleção (usada nas exclusões)"""
    def __init__(self, bot_instance, guild_id, items_list: list, placeholder: str, custom_id: str):
        super().__init__(bot_instance)
        self.guild_id = str(guild_id)
        self.items_list = items_list
        self.add_selection(items_list, placeholder, custom_id)
    
    def add_selection(self, items_list: list, placeholder: str, custom_id: str):
        if items_list:
            # Limita a 25 itens (limite do Discord)
            options = [discord.SelectOption(label=str(item), value=str(item), emoji="🗑️") for item in items_list[:25]]
            select = ui.Select(placeholder=placeholder, options=options, custom_id=custom_id)
            select.callback = self.on_select
            self.add_item(select)
        
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary, row=1, emoji="✖️")
        btn_cancel.callback = self.on_cancel
        self.add_item(btn_cancel)
    
    async def on_select(self, interaction: discord.Interaction):
        pass
    
    async def on_cancel(self, interaction: discord.Interaction):
        # Retorna para o painel de gerenciamento
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot, self.guild_id),
            view=PainelGerenciamento(self.bot, self.guild_id)
        )

# --- SETUP INICIAL ---
class PainelSetup(ui.View):
    def __init__(self, bot_instance, guild_id):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.guild_id = str(guild_id)
        
        my_cfg = get_config(self.guild_id).get("setup", {})

        self.selections = {
            "id_cargo_adm": my_cfg.get("id_cargo_adm"),
            "id_canal_comandos": my_cfg.get("id_canal_comandos"),
            "id_canal_countdown": my_cfg.get("id_canal_countdown")
        }

    @ui.select(cls=ui.RoleSelect, placeholder="👑 Selecione o Cargo de ADMIN", min_values=1, max_values=1, row=0)
    async def select_adm(self, interaction: discord.Interaction, select: ui.RoleSelect):
        self.selections["id_cargo_adm"] = select.values[0].id
        await interaction.response.defer()

    @ui.select(cls=ui.ChannelSelect, placeholder="💻 Canal do Painel/Comandos", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=1)
    async def select_cmd_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        self.selections["id_canal_comandos"] = select.values[0].id
        await interaction.response.defer()

    @ui.select(cls=ui.ChannelSelect, placeholder="⏳ Canal do Cronômetro", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=2)
    async def select_time_channel(self, interaction: discord.Interaction, select: ui.ChannelSelect):
        self.selections["id_canal_countdown"] = select.values[0].id
        await interaction.response.defer()

    @ui.button(label="Salvar Configuração", style=discord.ButtonStyle.success, emoji="💾", row=3)
    async def btn_save(self, interaction: discord.Interaction, button: ui.Button):
        if not all(self.selections.values()):
            await interaction.response.send_message("⚠️ Selecione todas as opções.", ephemeral=True)
            return

        def update_logic(data):
            if "setup" not in data: data["setup"] = {}
            data["setup"].update(self.selections)
            return data

        await update_config(self.guild_id, update_logic)
        
        embed = discord.Embed(title="✅ Configuração Salva!", description="Bot configurado para este servidor.", color=0x2ecc71)
        embed.add_field(name="Cargo Admin", value=f"<@&{self.selections['id_cargo_adm']}>")
        embed.set_footer(text="Agora use /painel")
        await interaction.response.edit_message(content=None, embed=embed, view=None)

# --- VIEWS DE GERENCIAMENTO (EXCLUSÃO) ---
class ExcluirOrgaoView(BaseSelectionView):
    def __init__(self, bot, guild_id):
        data = get_categories(guild_id)
        orgaos = sorted(list(data.get("orgaos", {}).keys()))
        super().__init__(bot, guild_id, orgaos, "Selecione o Orgão para APAGAR", "del_orgao")

    async def on_select(self, interaction: discord.Interaction):
        valor = interaction.data['values'][0]
        def remove(data):
            if valor in data["orgaos"]: del data["orgaos"][valor]
            return data
        await update_categories(self.guild_id, remove)
        await interaction.response.edit_message(
            content=f"✅ Orgão **{valor}** excluído com sucesso!",
            view=PainelGerenciamento(self.bot, self.guild_id)
        )

class ExcluirEquipeView(BaseSelectionView):
    def __init__(self, bot, guild_id):
        data = get_categories(guild_id)
        equipes = sorted(data.get("equipes", []))
        super().__init__(bot, guild_id, equipes, "Selecione a Equipe para APAGAR", "del_equipe")

    async def on_select(self, interaction: discord.Interaction):
        valor = interaction.data['values'][0]
        def remove(data):
            if valor in data["equipes"]: data["equipes"].remove(valor)
            return data
        await update_categories(self.guild_id, remove)
        await interaction.response.edit_message(
            content=f"✅ Equipe **{valor}** excluída com sucesso!",
            view=PainelGerenciamento(self.bot, self.guild_id)
        )

class ExcluirCategoriaStep1View(BaseSelectionView):
    def __init__(self, bot, guild_id):
        data = get_categories(guild_id)
        orgaos = sorted(list(data.get("orgaos", {}).keys()))
        super().__init__(bot, guild_id, orgaos, "1️⃣ De qual Orgão é a categoria?", "del_cat_step1")

    async def on_select(self, interaction: discord.Interaction):
        orgao = interaction.data['values'][0]
        await interaction.response.edit_message(
            content=f"📂 Selecionado: **{orgao}**. Agora escolha a categoria para apagar:",
            view=ExcluirCategoriaStep2View(self.bot, self.guild_id, orgao)
        )

class ExcluirCategoriaStep2View(BaseSelectionView):
    def __init__(self, bot, guild_id, orgao):
        self.orgao = orgao
        data = get_categories(guild_id)
        cats = sorted(data.get("orgaos", {}).get(orgao, []))
        super().__init__(bot, guild_id, cats, f"Apagar categoria de {orgao}...", "del_cat_final")

    async def on_select(self, interaction: discord.Interaction):
        cat_val = interaction.data['values'][0]
        def remove(data):
            if self.orgao in data["orgaos"] and cat_val in data["orgaos"][self.orgao]:
                data["orgaos"][self.orgao].remove(cat_val)
            return data
        await update_categories(self.guild_id, remove)
        await interaction.response.edit_message(
            content=f"✅ Categoria **{cat_val}** removida de {self.orgao}!",
            view=PainelGerenciamento(self.bot, self.guild_id)
        )

# --- PAINEL DE GERENCIAMENTO (SUB-MENU) ---
class PainelGerenciamento(BaseView):
    def __init__(self, bot_instance, guild_id):
        super().__init__(bot_instance)
        self.guild_id = str(guild_id)

    @ui.button(label="Excluir Orgão", style=discord.ButtonStyle.danger, row=0, emoji="🏢")
    async def btn_del_orgao(self, interaction: discord.Interaction, button: ui.Button):
        view = ExcluirOrgaoView(self.bot, self.guild_id)
        if len(view.children) < 2: # Se só tiver o botão cancelar
            await interaction.response.send_message("⚠️ Não há orgãos para excluir.", ephemeral=True)
            return
        await interaction.response.edit_message(content="🗑️ **Excluir Orgão**", view=view, embed=None)

    @ui.button(label="Excluir Categoria", style=discord.ButtonStyle.danger, row=0, emoji="📂")
    async def btn_del_cat(self, interaction: discord.Interaction, button: ui.Button):
        view = ExcluirCategoriaStep1View(self.bot, self.guild_id)
        if len(view.children) < 2:
            await interaction.response.send_message("⚠️ Não há dados suficientes.", ephemeral=True)
            return
        await interaction.response.edit_message(content="🗑️ **Excluir Categoria**", view=view, embed=None)

    @ui.button(label="Excluir Equipe", style=discord.ButtonStyle.danger, row=0, emoji="🛠️")
    async def btn_del_equipe(self, interaction: discord.Interaction, button: ui.Button):
        view = ExcluirEquipeView(self.bot, self.guild_id)
        if len(view.children) < 2:
            await interaction.response.send_message("⚠️ Não há equipes para excluir.", ephemeral=True)
            return
        await interaction.response.edit_message(content="🗑️ **Excluir Equipe**", view=view, embed=None)

    @ui.button(label="Voltar", style=discord.ButtonStyle.secondary, row=1, emoji="↩️")
    async def btn_back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot, self.guild_id),
            view=PainelPrincipal(self.bot, self.guild_id)
        )

# --- PAINEL DE PERMISSÕES ---
class PainelPermissoes(BaseView):
    PAGE_INFO = {
        1: {"title": "🛡️ Permissões - Página 1/2", "keys": ["extracao_canal", "extracao_tudo"], "labels": ["📦 Extração Canal", "🌎 Extração Global"]},
        2: {"title": "🛡️ Permissões - Página 2/2", "keys": ["resolvido", "reabrir"], "labels": ["✅ Finalizar Chamado", "🔓 Reabrir Chamado"]}
    }

    def __init__(self, bot_instance, guild_id):
        super().__init__(bot_instance)
        self.guild_id = str(guild_id)
        self.page = 1
        # Carrega permissões atuais DESTE servidor
        self.temp_perms = get_config(self.guild_id).get("perms", {}).copy()
        self.update_components()

    def get_defaults(self, key: str) -> list:
        ids = self.temp_perms.get(key, [])
        return [discord.Object(id=i) for i in ids]

    def build_status_embed(self) -> discord.Embed:
        info = self.PAGE_INFO[self.page]
        embed = discord.Embed(title=info["title"], description="Defina cargos para cada função.", color=0x3498db)
        for key, label in zip(info["keys"], info["labels"]):
            ids = self.temp_perms.get(key, [])
            valor = ", ".join([f"<@&{i}>" for i in ids]) if ids else "❌ *Ninguém*"
            embed.add_field(name=label, value=valor, inline=False)
        embed.set_footer(text=f"Página {self.page}/2")
        return embed

    def update_components(self):
        self.clear_items()
        info = self.PAGE_INFO[self.page]
        
        for key, label in zip(info["keys"], info["labels"]):
            select = ui.RoleSelect(placeholder=label, min_values=0, max_values=20, default_values=self.get_defaults(key))
            select.callback = self._create_callback(key)
            self.add_item(select)
        
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
        def update_perms(data):
            data["perms"] = self.temp_perms
            return data
        await update_config(self.guild_id, update_perms)
        await interaction.response.edit_message(
            content="✅ **Permissões Atualizadas!**",
            embed=build_dashboard_embed(self.bot, self.guild_id),
            view=PainelPrincipal(self.bot, self.guild_id)
        )

    async def _on_home(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_dashboard_embed(self.bot, self.guild_id),
            view=PainelPrincipal(self.bot, self.guild_id)
        )

# --- SELETOR DE CANAIS ---
class SeletorCanaisView(BaseView):
    def __init__(self, current_ids, bot, guild_id):
        super().__init__(bot)
        self.guild_id = str(guild_id)
        select = ui.ChannelSelect(
            placeholder="Selecione canais para backup...",
            channel_types=[discord.ChannelType.text],
            min_values=0, max_values=25,
            default_values=[discord.Object(id=int(i)) for i in current_ids]
        )
        select.callback = self._on_select
        self.add_item(select)
        
        btn = ui.Button(label="Voltar", style=discord.ButtonStyle.secondary, row=1, emoji="↩️")
        btn.callback = self.on_back
        self.add_item(btn)

    async def _on_select(self, interaction: discord.Interaction):
        novos = [str(x) for x in interaction.data['values']]
        def update_channels(data):
            antigos = data.get("connected_channels", {})
            novo_dict = {}
            for cid in novos:
                novo_dict[cid] = antigos.get(cid, {"last_marker_timestamp": datetime.min.replace(tzinfo=BRT_OFFSET).isoformat()})
            data["connected_channels"] = novo_dict
            return data
        await update_config(self.guild_id, update_channels)
        await interaction.response.edit_message(
            content="✅ **Canais atualizados!**",
            embed=build_dashboard_embed(self.bot, self.guild_id),
            view=PainelPrincipal(self.bot, self.guild_id)
        )

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=build_dashboard_embed(self.bot, self.guild_id),
            view=PainelPrincipal(self.bot, self.guild_id)
        )

# --- MODAIS ---
class NovoOrgaoModal(ui.Modal, title="Novo Orgão"):
    nome = ui.TextInput(label="Nome", max_length=50)
    def __init__(self, view_origin):
        super().__init__()
        self.view_origin = view_origin
    async def on_submit(self, interaction: discord.Interaction):
        nome_clean = sanitize_input(self.nome.value)
        def add(data):
            if nome_clean and nome_clean not in data["orgaos"]: data["orgaos"][nome_clean] = []
            return data
        await update_categories(self.view_origin.guild_id, add)
        self.view_origin.add_orgao_select()
        await interaction.response.edit_message(view=self.view_origin)

class NovaCategoriaModal(ui.Modal, title="Nova Categoria"):
    nome = ui.TextInput(label="Nome", max_length=50)
    def __init__(self, view_origin):
        super().__init__()
        self.view_origin = view_origin
    async def on_submit(self, interaction: discord.Interaction):
        nome_clean = sanitize_input(self.nome.value)
        orgao = self.view_origin.selections["orgao"]
        def add(data):
            if orgao in data["orgaos"] and nome_clean not in data["orgaos"][orgao]:
                data["orgaos"][orgao].append(nome_clean)
            return data
        await update_categories(self.view_origin.guild_id, add)
        self.view_origin.selections["categoria"] = nome_clean
        await self.view_origin.add_equipe_select(interaction)

class NovaEquipeModal(ui.Modal, title="Nova Equipe"):
    nome = ui.TextInput(label="Nome", max_length=50)
    def __init__(self, view_origin):
        super().__init__()
        self.view_origin = view_origin
    async def on_submit(self, interaction: discord.Interaction):
        nome_clean = sanitize_input(self.nome.value)
        def add(data):
            if nome_clean and nome_clean not in data["equipes"]: data["equipes"].append(nome_clean)
            return data
        await update_categories(self.view_origin.guild_id, add)
        self.view_origin.selections["quem_tratou"] = nome_clean
        await self.view_origin.finalizar_processo(interaction)

# --- PAINEL DE RESOLUÇÃO (WIZARD) ---
class PainelResolucao(ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = str(guild_id)
        self.selections = {"orgao": None, "categoria": None, "quem_tratou": None}
        self.add_orgao_select()

    # --- PASSO 1: ORGÃO ---
    def add_orgao_select(self):
        self.clear_items()
        data = get_categories(self.guild_id)
        orgaos_list = list(data.get("orgaos", {}).keys())
        orgaos_list.sort()
        
        if orgaos_list:
            options = [discord.SelectOption(label=o, value=o) for o in orgaos_list[:25]]
            select = ui.Select(placeholder="1️⃣ Selecione o Orgão...", options=options, row=0)
            select.callback = self.callback_orgao
            self.add_item(select)
        
        btn_novo = ui.Button(label="Criar Orgão", style=discord.ButtonStyle.success, row=1, emoji="➕")
        btn_novo.callback = self.btn_novo_orgao
        self.add_item(btn_novo)
        
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.danger, row=1, emoji="✖️")
        btn_cancel.callback = self.callback_cancelar
        self.add_item(btn_cancel)

    async def callback_orgao(self, interaction: discord.Interaction):
        self.selections["orgao"] = interaction.data['values'][0]
        await self.add_categoria_select(interaction)

    async def btn_novo_orgao(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NovoOrgaoModal(self))

    # --- PASSO 2: CATEGORIA ---
    async def add_categoria_select(self, interaction: discord.Interaction):
        self.clear_items()
        data = get_categories(self.guild_id)
        cats = data.get("orgaos", {}).get(self.selections["orgao"], [])
        cats.sort()
        
        if cats:
            options = [discord.SelectOption(label=c, value=c) for c in cats[:25]]
            select = ui.Select(placeholder="2️⃣ Selecione a Categoria...", options=options, row=0)
            select.callback = self.callback_cat
            self.add_item(select)
        
        btn_novo = ui.Button(label="Criar Categoria", style=discord.ButtonStyle.success, row=1, emoji="➕")
        btn_novo.callback = self.btn_nova_cat
        self.add_item(btn_novo)
        
        # Botões de navegação
        btn_back = ui.Button(label="Voltar", style=discord.ButtonStyle.secondary, row=2, emoji="⬅️")
        btn_back.callback = self.callback_voltar_orgao
        self.add_item(btn_back)
        
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.danger, row=2, emoji="✖️")
        btn_cancel.callback = self.callback_cancelar
        self.add_item(btn_cancel)
        
        msg = f"🏢 Orgão: **{self.selections['orgao']}**"
        if interaction.response.is_done(): await interaction.edit_original_response(content=msg, view=self)
        else: await interaction.response.edit_message(content=msg, view=self)

    async def btn_nova_cat(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NovaCategoriaModal(self))

    async def callback_cat(self, interaction: discord.Interaction):
        self.selections["categoria"] = interaction.data['values'][0]
        await self.add_equipe_select(interaction)

    # --- PASSO 3: EQUIPE (Modificado para incluir Dev/Processos) ---
    async def add_equipe_select(self, interaction: discord.Interaction):
        self.clear_items()
        data = get_categories(self.guild_id)
        equipes = data.get("equipes", [])
        
        # Garante que Dev e Processos estão na lista
        defaults = ["Dev", "Processos"]
        for d in defaults:
            if d not in equipes:
                equipes.append(d)
        
        equipes.sort()
        
        if equipes:
            options = [discord.SelectOption(label=e, value=e) for e in equipes[:25]]
            select = ui.Select(placeholder="3️⃣ Quem resolveu?", options=options, row=0)
            select.callback = self.callback_equipe
            self.add_item(select)
            
        btn_novo = ui.Button(label="Criar Equipe", style=discord.ButtonStyle.primary, row=1, emoji="➕")
        btn_novo.callback = self.btn_nova_equipe
        self.add_item(btn_novo)
        
        # Botões de navegação
        btn_back = ui.Button(label="Voltar", style=discord.ButtonStyle.secondary, row=2, emoji="⬅️")
        btn_back.callback = self.callback_voltar_categoria
        self.add_item(btn_back)
        
        btn_cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.danger, row=2, emoji="✖️")
        btn_cancel.callback = self.callback_cancelar
        self.add_item(btn_cancel)
        
        txt = f"🏢 Orgão: **{self.selections['orgao']}**\n📂 Categoria: **{self.selections['categoria']}**"
        if interaction.response.is_done(): await interaction.edit_original_response(content=txt, view=self)
        else: await interaction.response.edit_message(content=txt, view=self)

    async def btn_nova_equipe(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NovaEquipeModal(self))

    async def callback_equipe(self, interaction: discord.Interaction):
        self.selections["quem_tratou"] = interaction.data['values'][0]
        await self.finalizar_processo(interaction)

    # --- CALLBACKS DE NAVEGAÇÃO ---
    async def callback_cancelar(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="🚫 Processo cancelado.", view=None)

    async def callback_voltar_orgao(self, interaction: discord.Interaction):
        self.add_orgao_select()
        await interaction.response.edit_message(content="📂 Selecione o Orgão:", view=self)

    async def callback_voltar_categoria(self, interaction: discord.Interaction):
        # Para voltar para categoria, precisamos resetar a seleção dela
        self.selections["categoria"] = None
        # E reexibir o menu de categorias baseado no orgão já selecionado
        await self.add_categoria_select(interaction)

    async def finalizar_processo(self, interaction: discord.Interaction):
        from extraction import finalizar_topico_logica
        await finalizar_topico_logica(interaction, self.selections, self.guild_id)

# --- PAINEL PRINCIPAL (HOME) ---
class PainelPrincipal(BaseView):
    """Tela principal com todos os botões (restaurados)"""
    def __init__(self, bot_instance, guild_id):
        super().__init__(bot_instance)
        self.guild_id = str(guild_id)
        self.last_backup_click = 0 # Controle de cooldown local para o botão
    
    @ui.button(label="Canais", style=discord.ButtonStyle.secondary, row=0, emoji="📡")
    async def btn_canais(self, interaction: discord.Interaction, button: ui.Button):
        cfg = get_config(self.guild_id)
        ids = list(cfg.get("connected_channels", {}).keys())
        embed = discord.Embed(title="📡 Gerir Canais", description="Selecione abaixo os canais para monitorar.", color=0x3498db)
        await interaction.response.edit_message(embed=embed, view=SeletorCanaisView(ids, self.bot, self.guild_id))

    @ui.button(label="Permissões", style=discord.ButtonStyle.secondary, row=0, emoji="🛡️")
    async def btn_perms(self, interaction: discord.Interaction, button: ui.Button):
        view = PainelPermissoes(self.bot, self.guild_id)
        await interaction.response.edit_message(content=None, embed=view.build_status_embed(), view=view)

    @ui.button(label="Configurações", style=discord.ButtonStyle.secondary, row=0, emoji="⚙️")
    async def btn_config(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=build_dashboard_embed(self.bot, self.guild_id), view=PainelGerenciamento(self.bot, self.guild_id))

    @ui.button(label="Forçar Backup", style=discord.ButtonStyle.primary, row=1, emoji="💾")
    async def btn_backup(self, interaction: discord.Interaction, button: ui.Button):
        # Importa aqui para evitar ciclo
        from extraction import perform_extraction_guild
        
        now = datetime.now().timestamp()
        if now - self.last_backup_click < 30: # 5 minutos cooldown
            await interaction.response.send_message(f"⏳ Aguarde {int(30-(now-self.last_backup_click))}s.", ephemeral=True)
            return
            
        self.last_backup_click = now
        await interaction.response.defer(ephemeral=True)
        
        try:
            stats, zip_p = await perform_extraction_guild(self.bot, self.guild_id)
            msg = f"✅ **Backup Manual!** Novos: {stats['topicos']}"
            if zip_p: await interaction.followup.send(msg, file=discord.File(zip_p), ephemeral=True)
            else: await interaction.followup.send(msg + "\n(Sem arquivos novos)", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

    @ui.button(label="Sair", style=discord.ButtonStyle.danger, row=1, emoji="✖️")
    async def btn_close(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="👋 Painel fechado.", view=None, embed=None)

def build_dashboard_embed(bot_inst, guild_id) -> discord.Embed:
    cfg = get_config(str(guild_id))
    con_channels = cfg.get("connected_channels", {})
    count = len(con_channels)
    lista = []
    for cid in con_channels:
        ch = bot_inst.get_channel(int(cid))
        lista.append(f"• {ch.name}" if ch else f"• ID {cid}")
    txt_canais = "\n".join(lista[:5])
    if count > 5: txt_canais += f"\n... e mais {count-5}"
    if not txt_canais: txt_canais = "Nenhum canal configurado."

    next_run = datetime.now(BRT_OFFSET).replace(hour=HORA_BACKUP, minute=MINUTO_BACKUP, second=0)
    if datetime.now(BRT_OFFSET) > next_run: next_run += timedelta(days=1)
    ts_next = int(next_run.timestamp())
    
    embed = discord.Embed(title="🎛️ Painel de Controle", description="Sistema de Gestão (Multi-Server)", color=0x5865F2)
    embed.add_field(name=f"🟢 Status", value=f"**Ping:** `{int(bot_inst.latency*1000)}ms`\n**Online:** Sim", inline=True)
    embed.add_field(name="⏳ Próximo Backup", value=f"<t:{ts_next}:R>", inline=True)
    embed.add_field(name="", value="⠀", inline=False)
    embed.add_field(name="📡 Canais Monitorados", value=f"Total: {count}\n{txt_canais}", inline=False)
    embed.set_footer(text=f"Servidor: {guild_id}")
    return embed