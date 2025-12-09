# AIA-BOT

### Como usar

# Ágape IA - Bot Extrator de Chat para Discord

Este é um bot para Discord, construído em Python com a biblioteca `discord.py`, projetado para automatizar a extração de históricos de conversas de canais de texto. Ele salva mensagens, anexos e tópicos (threads) em arquivos JSON locais, organizados por data e hora.

## 🚀 Funcionalidades

* **Extração Automática:** Executa uma extração completa de todos os canais conectados diariamente às **23:59 (BRT / UTC-3)**.
* **Extração Manual:** Permite que administradores iniciem uma extração a qualquer momento usando o comando `/extrairconversa`.
* **Extração Inteligente (Timestamp):** O bot registra a hora da última extração e, nas execuções seguintes, busca apenas por mensagens *novas* enviadas desde então.
* **Processamento Completo de Tópicos (Threads):**
    * Detecta e extrai *novos tópicos* criados.
    * Detecta *novas mensagens em tópicos antigos* e re-extrai o tópico **inteiro** para garantir um snapshot completo.
* **Download de Anexos:** Baixa e salva localmente todos os arquivos e imagens enviados nas mensagens e tópicos, referenciando-os no JSON.
* **Contador Regressivo:** Exibe um contador regressivo em tempo real em um canal dedicado, mostrando o tempo exato para a próxima extração automática.
* **Controle de Acesso:** Os comandos de administração são restritos a um `ID_CARGO_ADM` específico e só podem ser usados em um `ID_CANAL_COMANDOS` dedicado.
* **Padronização de Fuso Horário:** Todos os timestamps (nomes de pastas, logs no chat e dados nos JSONs) são padronizados para o fuso horário **BRT (UTC-3)**.

---

## 🔧 Configuração e Instalação

Siga estes passos para rodar o bot.

### 1. Pré-requisitos

* Python 3.10 ou superior
* Uma conta de Bot no [Portal de Desenvolvedores do Discord](https://discord.com/developers/applications)

### 2. Instalação das Bibliotecas

Clone ou baixe este repositório e instale as dependências necessárias:

```bash
pip install discord.py
pip install aiohttp
pip install python-dotenv

### 3\. Configuração de Permissões (Intents)

No Portal de Desenvolvedores do Discord, vá até a aba "Bot" do seu aplicativo e **ative** as seguintes "Privileged Gateway Intents":

  * **[ATIVADO] SERVER MEMBERS INTENT**
  * **[ATIVADO] MESSAGE CONTENT INTENT**

### 4\. Configuração das Variáveis

O bot usa um arquivo `.env` para armazenar o token e IDs de configuração.

**a. Crie o arquivo `.env`**
Na pasta raiz do projeto, crie um arquivo chamado `.env`.

**b. Adicione seu Token**
Adicione seu token secreto do Discord (do Portal de Desenvolvedores) ao arquivo `.env`:

```
DISCORD_TOKEN=SEU_TOKEN_SECRETO_VAI_AQUI
```

**c. Configure os IDs no `bot.py`**
Abra o arquivo `bot.py` e configure os seguintes IDs no topo do arquivo:

```python
# IDs
ID_CARGO_ADM = "1440018537077805189"         # ID do Cargo que pode usar os comandos
ID_CANAL_COMANDOS = "1440031095310782515"  # ID do Canal onde /conectar e /desconectar funcionam
COUNTDOWN_CHANNEL_ID = 1440035660814749748   # ID do Canal do cronômetro
```

### 5\. Arquivos de Configuração

  * **`.gitignore`:** Este arquivo garante que seus segredos (`.env`), seus dados (`extracoes/`) e seu arquivo de estado (`config.json`) **nunca** sejam enviados para o GitHub.
  * **`config.json`:** Este arquivo é **criado automaticamente** pelo bot na primeira execução. Ele armazena quais canais estão conectados e o timestamp da última extração de cada um.

### 6\. Executando o Bot

Após configurar tudo, inicie o bot:

```bash
python bot.py
```

-----

## 🤖 Comandos de Uso

Os comandos de administração só podem ser usados por membros com o `ID_CARGO_ADM`.

  * `/conectar [canal]`

      * **Onde usar:** Apenas no Canal de Comandos.
      * **O que faz:** Adiciona um canal à lista de extração automática e manual. O bot começará a monitorá-lo.

  * `/desconectar [canal]`

      * **Onde usar:** Apenas no Canal de Comandos.
      * **O que faz:** Remove um canal da lista de extração.

  * `/extrairconversa [canal]`

      * **Onde usar:** Pode ser usado de qualquer canal (desde que o usuário seja ADM).
      * **O que faz:** Inicia imediatamente uma extração manual de um canal conectado.

-----

## 📁 Estrutura dos Arquivos de Extração

Todas as extrações são salvas na pasta `./extracoes/`, seguindo esta estrutura:

```
extracoes/
└── nome-do-canal_ID-DO-CANAL/
    └── 2025-11-17_15-30-00/                   (Timestamp da extração em BRT)
        ├── arquivos_canal/
        │   └── anexo_do_canal.png
        ├── topico_ID-DO-TOPICO_nome-do-topico/
        │   ├── arquivos/
        │   │   └── anexo_do_topico.jpg
        │   └── historico_topico.json
        └── historico_chat.json
```

  * **`historico_chat.json`**: Contém o JSON com as mensagens do canal principal, referências a tópicos novos e referências a tópicos atualizados.
  * **`historico_topico.json`**: Contém o histórico *completo* daquele tópico específico.

<!-- end list -->

```
```
