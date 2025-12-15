"""
config.py - Gerenciamento de dados isolados por servidor (Guild Sharding)
"""
import discord
from discord.ext import commands
import asyncio
import os
import json
import re
import tempfile
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURAÇÃO GERAL ---
HORA_BACKUP = 23      
MINUTO_BACKUP = 59  
BRT_OFFSET = timezone(timedelta(hours=-3))
BASE_DATA_PATH = "./dados_servidores" # Pasta raiz para todos os dados

# --- LOCKS PARA OPERAÇÕES ASYNC ---
_GUILD_LOCKS = {}

def get_guild_lock(guild_id: str) -> asyncio.Lock:
    if guild_id not in _GUILD_LOCKS:
        _GUILD_LOCKS[guild_id] = asyncio.Lock()
    return _GUILD_LOCKS[guild_id]

# --- GERENCIAMENTO DE ARQUIVOS (CAMADA DE ISOLAMENTO) ---

class DataManager:
    """Gerenciador que garante que dados do Server A não toquem no Server B"""
    
    @staticmethod
    def get_path(guild_id: str, filename: str) -> str:
        """Gera o caminho ./dados_servidores/{guild_id}/{filename}"""
        guild_id = str(guild_id)
        folder = os.path.join(BASE_DATA_PATH, guild_id)
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, filename)

    @staticmethod
    def load_json(guild_id: str, filename: str, default_data: dict) -> dict:
        """Lê JSON de forma síncrona (para uso interno ou getters simples)"""
        path = DataManager.get_path(guild_id, filename)
        if not os.path.exists(path):
            DataManager.save_sync(path, default_data)
            return default_data
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️ Arquivo corrompido detectado: {path}. Retornando padrão.")
            return default_data

    @staticmethod
    def save_sync(filepath: str, data: dict) -> None:
        """
        Escreve JSON de forma ATÔMICA (Safe Write).
        1. Escreve num arquivo temporário.
        2. Renomeia para o arquivo final.
        """
        dir_name = os.path.dirname(filepath)
        # Cria arquivo temporário na mesma pasta
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, indent=4, ensure_ascii=False)
            tmp_file.flush()
            os.fsync(tmp_file.fileno()) # Garante que foi escrito no disco
            tmp_path = tmp_file.name

        # Substituição atômica
        try:
            os.replace(tmp_path, filepath)
        except OSError as e:
            print(f"❌ Erro ao salvar arquivo {filepath}: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @staticmethod
    async def save_guild_data(guild_id: str, filename: str, data: dict) -> None:
        """
        Salva dados de forma assíncrona.
        """
        lock = get_guild_lock(str(guild_id))
        path = DataManager.get_path(guild_id, filename)
        
        async with lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, DataManager.save_sync, path, data)

# --- FUNÇÕES DE ACESSO A DADOS (GETTERS) ---

def get_config(guild_id: str) -> dict:
    """Retorna a config APENAS do servidor solicitado"""
    if not guild_id: return {}
    
    default = {
        "setup": {
            "id_cargo_adm": None,
            "id_canal_comandos": None,
            "id_canal_countdown": None,
            "id_canal_aprovacao": None  # Novo: Canal para enviar embeds de aprovação
        },
        "connected_channels": {},
        "perms": {
            "extracao_canal": [],
            "extracao_tudo": [],    
            "reabrir": [],
            "resolvido": [],
            "aprovar": [] # Novo: Quem pode clicar no botão Aprovar/Reprovar
        }
    }
    return DataManager.load_json(str(guild_id), "config.json", default)

def get_categories(guild_id: str) -> dict:
    """Retorna as categorias/equipes APENAS do servidor solicitado"""
    if not guild_id: return {}
    
    default = {
        "orgaos": {},
        "equipes": ["Geral", "TI"] 
    }
    return DataManager.load_json(str(guild_id), "categorias.json", default)

def get_setup_id(guild_id: int, key: str) -> int:
    """Busca um ID de configuração específico"""
    if not guild_id: return None
    cfg = get_config(str(guild_id))
    val = cfg.get("setup", {}).get(key)
    return int(val) if val else None

# --- FUNÇÕES DE ATUALIZAÇÃO SEGURA (CORRIGIDAS) ---

async def update_config(guild_id: str, modification_callback):
    """Atualiza a configuração de um servidor atomicamente"""
    lock = get_guild_lock(str(guild_id))
    
    def _sync_update():
        current = get_config(guild_id) # Lê
        new_data = modification_callback(current) # Modifica
        DataManager.save_sync(DataManager.get_path(guild_id, "config.json"), new_data) # Salva
        return new_data

    async with lock:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_update)

async def update_categories(guild_id: str, modification_callback):
    """Atualiza categorias de um servidor atomicamente"""
    lock = get_guild_lock(str(guild_id))
    
    def _sync_update():
        current = get_categories(guild_id)
        new_data = modification_callback(current)
        DataManager.save_sync(DataManager.get_path(guild_id, "categorias.json"), new_data)
        return new_data

    async with lock:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_update)

# --- GERENCIAMENTO DE PENDÊNCIAS (NOVO) ---

async def log_pending_safe(guild_id: str, thread_id: int, thread_name: str, 
                           resolvido_por: str, resolvido_por_id: int, 
                           categoria: str, orgao: str, canal_origem: str) -> None:
    """Adiciona um tópico à lista de pendências de aprovação"""
    lock = get_guild_lock(str(guild_id))
    
    def _sync_log():
        path = DataManager.get_path(guild_id, "pendencias.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except:
            db = []
        
        # Cria entrada
        new_data = {
            "data_solicitacao": datetime.now(BRT_OFFSET).isoformat(),
            "thread_id": str(thread_id),
            "thread_nome": thread_name,
            "canal_origem": canal_origem,
            "resolvido_por": resolvido_por,
            "resolvido_por_id": str(resolvido_por_id),
            "orgao": orgao,
            "categoria": categoria
        }
        
        # Remove se já existir (atualização)
        db = [entry for entry in db if entry.get("thread_id") != str(thread_id)]
        db.append(new_data)
        
        DataManager.save_sync(path, db)

    async with lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_log)

async def get_pending_data(guild_id: str, thread_id: int) -> dict:
    """Recupera dados de uma pendência específica"""
    path = DataManager.get_path(guild_id, "pendencias.json")
    if not os.path.exists(path): return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
        return next((item for item in db if item["thread_id"] == str(thread_id)), None)
    except:
        return None

async def remove_pending_safe(guild_id: str, thread_id: int) -> None:
    """Remove um tópico da lista de pendências"""
    lock = get_guild_lock(str(guild_id))
    
    def _sync_remove():
        path = DataManager.get_path(guild_id, "pendencias.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
            
            novo_db = [entry for entry in db if entry.get("thread_id") != str(thread_id)]
            
            if len(novo_db) < len(db):
                DataManager.save_sync(path, novo_db)
        except:
            pass

    async with lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_remove)

# --- GERENCIAMENTO DE RESOLUÇÕES (FINALIZADAS) ---

async def log_resolution_safe(guild_id: str, thread_id: int, thread_name: str, 
                            resolvido_por: str, resolvido_por_id: int, 
                            categoria: str, orgao: str) -> None:
    """Salva a resolução definitiva atomicamente (após aprovação)"""
    lock = get_guild_lock(str(guild_id))
    
    def _sync_log():
        path = DataManager.get_path(guild_id, "resolucoes.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except:
            db = []
        
        # Atualiza ou cria entrada
        entry = next((i for i in db if i.get("thread_id") == str(thread_id)), None)
        new_data = {
            "data": datetime.now(BRT_OFFSET).isoformat(),
            "thread_id": str(thread_id),
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
            
        DataManager.save_sync(path, db)

    async with lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_log)

async def remove_resolution(guild_id: str, thread_id: int) -> bool:
    """Remove entrada de resolução do banco atomicamente"""
    lock = get_guild_lock(str(guild_id))
    
    def _sync_remove():
        path = DataManager.get_path(guild_id, "resolucoes.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
            
            novo_db = [entry for entry in db if entry.get("thread_id") != str(thread_id)]
            
            if len(novo_db) < len(db):
                DataManager.save_sync(path, novo_db)
                return True
        except:
            pass
        return False

    async with lock:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_remove)

async def registrar_log_safe(guild_id: str, acao: str, usuario: str, detalhes: str) -> None:
    """Registra log no arquivo do servidor atomicamente"""
    if not guild_id: return
    lock = get_guild_lock(str(guild_id))
    
    def _sync_log():
        path = DataManager.get_path(guild_id, "logs.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except:
            logs = []
            
        logs.append({
            "timestamp": datetime.now(BRT_OFFSET).isoformat(),
            "acao": acao,
            "usuario": usuario,
            "detalhes": detalhes
        })
        
        # Rotação de logs
        if len(logs) > 1000:
            logs = logs[-1000:]
            
        DataManager.save_sync(path, logs)

    async with lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_log)

# --- UTILITÁRIOS GERAIS ---
def sanitize_input(texto: str, max_len: int = 50) -> str:
    if not texto: return ""
    limpo = re.sub(r'[^\w\s\-\.]', '', texto)
    return limpo[:max_len].strip()

def clean_name(nome: str) -> str:
    return "".join(c for c in nome if c.isalnum() or c in ('-', '_', ' ')).strip().replace(' ', '_')

async def execute_with_retry(bot, funcao, *args, tentativas=3, delay=2, **kwargs):
    for i in range(tentativas):
        try:
            return await funcao(*args, **kwargs)
        except Exception as e:
            if i == tentativas - 1: raise e
            await asyncio.sleep(delay)

def get_all_active_guilds():
    """Retorna lista de IDs de servidores que possuem pasta de dados"""
    if not os.path.exists(BASE_DATA_PATH): return []
    return [d for d in os.listdir(BASE_DATA_PATH) if os.path.isdir(os.path.join(BASE_DATA_PATH, d)) and d.isdigit()]