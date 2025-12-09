"""
main.py - Arquivo principal do bot AMANDa (Multi-Server)
Responsável por inicializar o bot, carregar extensões e gerir o ciclo de vida.
"""
import discord
from discord.ext import commands
import os
import traceback
from dotenv import load_dotenv

# Importações dos módulos locais
# Adicionamos setup_events aqui e removemos importações antigas
from extraction import setup_commands, setup_events, set_bot, daily_extraction_loop, update_countdown_loop

# Carrega variáveis de ambiente (.env)
load_dotenv()

# --- CONFIGURAÇÃO DO BOT ---
intents = discord.Intents.default()
intents.guilds = True           # Necessário para gerir servidores
intents.messages = True         # Necessário para ler mensagens
intents.message_content = True  # Necessário para ler conteúdo de mensagens (comandos)
intents.members = True          # Necessário para verificar cargos/membros

bot = commands.Bot(command_prefix="!", intents=intents)

# --- EVENTOS GERAIS ---
@bot.event
async def on_ready():
    """Executado quando o bot fica online"""
    print(f"🚀 Bot iniciado como: {bot.user}")
    print(f"🆔 ID do Bot: {bot.user.id}")
    print("📦 Estrutura Multi-Server carregada e pronta.")
    
    # Sincroniza comandos Slash (App Commands) com o Discord
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} comandos Slash sincronizados.")
    except Exception as e:
        print(f"❌ Erro ao sincronizar comandos: {e}")
        traceback.print_exc()
        
    # Inicia loops de background (se já não estiverem a correr)
    if not daily_extraction_loop.is_running():
        daily_extraction_loop.start()
        print("⏰ Loop de extração diária iniciado.")
        
    if not update_countdown_loop.is_running():
        update_countdown_loop.start()
        print("⏳ Loop de countdown iniciado.")

# --- FUNÇÃO PRINCIPAL ---
def main():
    """Função de entrada"""
    
    # 1. Define a referência global do bot no módulo de extração
    set_bot(bot)
    
    # 2. Configura eventos (on_message para bloquear tópicos trancados)
    setup_events(bot)
    
    # 3. Registra os comandos (Slash Commands)
    setup_commands(bot)
    
    # 4. Obtém token do arquivo .env
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("\n❌ ERRO CRÍTICO: Token não encontrado!")
        print("Crie um arquivo chamado '.env' na raiz com o conteúdo: DISCORD_TOKEN=seutokenaqui")
        return
    
    # 5. Executa o bot
    print("🔄 A conectar ao Discord...")
    try:
        bot.run(token)
    except KeyboardInterrupt:
        print("\n⚠️ Bot interrompido pelo utilizador (Ctrl+C).")
    except discord.LoginFailure:
        print("\n❌ Erro de Login: O token fornecido é inválido.")
    except Exception as e:
        print(f"\n❌ Erro fatal ao executar bot: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()