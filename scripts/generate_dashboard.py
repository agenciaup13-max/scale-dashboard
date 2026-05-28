"""
Scale Dashboard — Generator
Roda toda quarta-feira às 05h00 BRT via GitHub Actions.

Fluxo:
  1. Para cada gestor, busca a página mais recente "Semana DD/MM..."
  2. Faz parse APENAS da tabela "Resumo da Carteira" para Verde/Amarelo/Vermelho.
     (A tabela é a ÚNICA fonte de verdade para contagem e nomes de clientes.)
  3. Para cada cliente não-verde, busca o bloco de detalhes no markdown por
     correspondência parcial de palavras no nome do cliente.
  4. Gera insight via Anthropic API.
  5. Grava docs/index.html com os dados embutidos.
"""

import os
import re
import sys
import json
import datetime
import requests

# ── deps condicionais ───────────────────────────────────────────────────────
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    print("⚠  anthropic não instalado — insights não serão gerados.", flush=True)

# ── CONFIG ──────────────────────────────────────────────────────────────────
WORKSPACE_ID = "9013858226"

MANAGERS = [
    {"name": "Erick",   "doc_id": "8cm93xj-34873"},
    {"name": "Edu",     "doc_id": "8cm93xj-34853"},
    {"name": "Adryon",  "doc_id": "8cm93xj-34893"},
    {"name": "Leandro", "doc_id": "8cm93xj-34913"},
    {"name": "Lucas",   "doc_id": "8cm93xj-34933"},
]

CLICKUP_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

HEADERS = {"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"}
BASE    = "https://api.clickup.com/api/v3"

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "index.html")


# ── CLICKUP HELPERS ─────────────────────────────────────────────────────────

def cu_get(path, params=None):
    url = BASE + path
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def list_pages(doc_id):
    """Retorna lista plana de todas as páginas do documento."""
    data = cu_get(f"/workspaces/{WORKSPACE_ID}/docs/{doc_id}/pages",
                  params={"max_page_depth": -1})
    pages = []

    def flatten(lst):
        if not isinstance(lst, list):
            return
        for p in lst:
            if not isinstance(p, dict):
                continue
            pages.append(p)
            if p.get("pages"):
                flatten(p["pages"])

    if isinstance(data, list):
        flatten(data)
    elif isinstance(data, dict):
        flatten(data.get("pages", []))
    return pages


def get_page_content(doc_id, page_id):
    data = cu_get(f"/workspaces/{WORKSPACE_ID}/docs/{doc_id}/pages/{page_id}",
                  params={"content_format": "text/md"})
    if isinstance(data, dict):
        return data.get("content", "")
    return ""


def find_latest_week_page(pages):
    """
    Retorna o (page_id, page_name) da página de semana mais recente.
    Ignora a página MODELO (DD/MM).
    """
    candidates = []
    modelo_re = re.compile(r"DD/MM", re.IGNORECASE)
    for p in pages:
        name = p.get("name", "")
        if "Semana" in name and not modelo_re.search(name):
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.get("date_updated", 0), reverse=True)
    return candidates[0]["id"], candidates[0]["name"]


# ── MARKDOWN PARSER ─────────────────────────────────────────────────────────

def parse_resumo_table(md: str):
    """
    Extrai APENAS a tabela dentro de '## Resumo da Carteira'.
    Retorna (verde_list, amarelo_list, vermelho_list).

    A tabela do Resumo é a ÚNICA fonte de verdade para contagem e nomes.
    Outras tabelas do documento (ex: Pendências) são completamente ignoradas.
    """
    # Extrai somente o bloco "## Resumo da Carteira" até o próximo ## ou * * *
    m = re.search(
        r'##\s+Resumo da Carteira\s*\n(.*?)(?=\n\*\s*\*\s*\*|\n---+\n|\n## |\Z)',
        md, re.DOTALL | re.IGNORECASE
    )
    if not m:
        print("  ⚠ Seção 'Resumo da Carteira' não encontrada.", flush=True)
        return [], [], []

    section = m.group(1)

    # Regex para linhas de tabela com pelo menos 3 colunas pipe-delimitadas
    row_re = re.compile(r'^\|(.+?)\|(.+?)\|(.+?)(?:\|.*)?$', re.MULTILINE)
    rows = row_re.findall(section)

    SKIP_WORDS = {"verdes", "amarelos", "vermelhos", "verde", "amarelo", "vermelho", ""}

    def split_names(cell: str) -> list:
        # <br> vira newline
        cell = re.sub(r'<br\s*/?>', '\n', cell, flags=re.IGNORECASE)
        # Separa por newline ou vírgula
        items = [i.strip() for i in re.split(r'[\n,]+', cell) if i.strip()]
        # Remove emojis e formatação markdown
        items = [re.sub(r'[🟢🟡🔴✅⏳❌_*\\]', '', i).strip() for i in items]
        # Remove colchetes e parênteses extras mantendo o conteúdo
        items = [re.sub(r'^[\[\(]+|[\]\)]+$', '', i).strip() for i in items]
        # Filtra cabeçalhos, separadores e vazios
        items = [i for i in items if i and i.lower().strip() not in SKIP_WORDS]
        return items

    verde, amarelo, vermelho = [], [], []
    for row in rows:
        v, a, r = [x.strip() for x in row]
        lv = v.lower()
        # Pula linha de cabeçalho
        if any(kw in lv for kw in ("verde", "amarelo", "vermelho")):
            continue
        # Pula linhas de separador (--- ou vazio)
        if re.fullmatch(r'[\s\-]+', v.replace('|', '')):
            continue
        verde   += split_names(v)
        amarelo += split_names(a)
        vermelho += split_names(r)

    return verde, amarelo, vermelho


def _normalize(s: str) -> str:
    """Remove caracteres especiais e acentos, retorna lowercase."""
    return re.sub(r'[^a-zA-Z0-9\s]', '', s.lower()).strip()


def _significant_words(name: str) -> list:
    """Retorna palavras com 3+ caracteres do nome normalizado (excluindo stopwords)."""
    STOPWORDS = {"the", "dos", "das", "del", "von", "van", "para", "com"}
    words = [w for w in _normalize(name).split() if len(w) >= 3 and w not in STOPWORDS]
    return words


def _extract_field(pattern, text):
    """Extrai um campo de texto markdown e limpa a formatação."""
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return "—"
    val = m.group(1)
    val = re.sub(r'^\*+', '', val).strip()
    val = re.split(r'\n\*\*', val)[0]
    val = re.sub(r'[\*_]{1,2}', '', val)
    val = re.sub(r'<!--.*?-->', '', val, flags=re.DOTALL)
    val = re.sub(r'!\[.*?\]\(.*?\)', '', val)   # remove imagens
    val = re.sub(r'\[[ xX]\]', '', val)          # remove checkboxes
    val = re.sub(r'[-*]\s+', ' ', val)
    val = re.sub(r'\n+', ' ', val).strip()
    val = re.sub(r'\s{2,}', ' ', val)
    return val if val else "—"


def _parse_client_block(block: str, nome: str) -> dict:
    """Extrai todos os campos estruturados de um bloco de cliente."""
    return {
        "nome":     nome,
        "funil":    _extract_field(
            r'\*\*Funil(?:\(is\))? afetado(?:\(s\))?[:\s]*\*\*\s*(.+?)(?=\n\*\*|\Z)', block),
        "problema": _extract_field(
            r'\*\*Qual o problema\?[:\s]*\*\*(.+?)(?=\n\*\*|\Z)', block),
        "dados":    _extract_field(
            r'\*\*O que os dados mostram\?[:\s]*\*\*(.+?)(?=\n\*\*|\Z)', block),
        "tentou":   _extract_field(
            r'\*\*O que (?:eu )?já tentei\?[:\s]*\*\*(.+?)(?=\n\*\*|\Z)', block),
        "sugestao": _extract_field(
            r'\*\*Minha sugestão de próximo passo[:\s]*\*\*(.+?)(?=\n\*\*|\Z)', block),
    }


def find_section_content(md: str, client_name: str) -> dict:
    """
    Busca a seção do cliente no markdown usando correspondência parcial de palavras.

    Estratégia:
    - Divide o MD em blocos de ## e ### sections
    - Para cada bloco de "cliente", verifica se alguma palavra significativa
      do nome do cliente aparece no texto do bloco (primeiros 500 chars)
    - Se encontrado, extrai os campos estruturados

    Isso resolve o problema de nomes diferentes entre tabela e seção
    (ex: tabela "VHE - L3" ↔ seção "### Cliente: VHE")
    """
    words = _significant_words(client_name)
    if not words:
        return {"nome": client_name, "funil": "—", "problema": "—",
                "dados": "—", "tentou": "—", "sugestao": "—"}

    # Divide em seções ## e ###
    raw_sections = re.split(r'(?m)^(?=#{2,3}\s)', md)

    best_match = None
    best_score = 0

    for sec in raw_sections:
        first_line = sec.split('\n', 1)[0].strip()
        # Só analisa seções relacionadas a clientes
        is_client_section = (
            'cliente' in first_line.lower() or
            any(emoji in first_line for emoji in ('🔴', '🟡', '🟢'))
        )
        if not is_client_section:
            continue

        # Normaliza os primeiros 600 chars do bloco para busca
        norm_sec = _normalize(sec[:600])
        # Conta quantas palavras significativas batem
        score = sum(1 for w in words if w in norm_sec)
        if score > best_score:
            best_score = score
            best_match = sec

    if best_match and best_score > 0:
        return _parse_client_block(best_match, client_name)

    print(f"    ⚠ Seção não encontrada para '{client_name}' — usando campos vazios.", flush=True)
    return {"nome": client_name, "funil": "—", "problema": "—",
            "dados": "—", "tentou": "—", "sugestao": "—"}


# ── ANTHROPIC INSIGHT ────────────────────────────────────────────────────────

def generate_insight(client: dict) -> str:
    if not HAS_ANTHROPIC or not ANTHROPIC_KEY:
        return (
            "Insight automático indisponível (ANTHROPIC_API_KEY não configurada). "
            "Configure o secret no GitHub para ativar os insights gerados por IA."
        )

    prompt = f"""Você é um especialista sênior em tráfego pago e funis de vendas digitais.
Analise o caso abaixo e gere um insight objetivo, direto e acionável em português brasileiro.
Diga claramente se a sugestão do gestor faz sentido ou não, e acrescente sua visão técnica.
Seja específico, cite métricas quando relevante. Máximo 4 frases. Sem markdown.

Cliente: {client['nome']}
Funil: {client['funil']}
Problema: {client['problema']}
Dados: {client['dados']}
Já tentou: {client['tentou']}
Sugestão do gestor: {client['sugestao']}"""

    try:
        ac = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = ac.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠ Insight API error: {e}", flush=True)
        return f"Erro ao gerar insight: {e}"


# ── MAIN DATA FETCH ──────────────────────────────────────────────────────────

def fetch_manager_data(mgr: dict) -> dict:
    name   = mgr["name"]
    doc_id = mgr["doc_id"]
    print(f"\n{'─'*40}\n📂 {name} — doc {doc_id}", flush=True)

    pages  = list_pages(doc_id)
    result = find_latest_week_page(pages)
    if not result:
        print(f"  ⚠ Nenhuma página de semana encontrada.", flush=True)
        return {"name": name, "week": None, "verde": [], "clients": []}

    page_id, page_name = result
    print(f"  📋 Página: {page_name} (id: {page_id})", flush=True)

    content = get_page_content(doc_id, page_id)

    # ── Tabela Resumo é a ÚNICA fonte de verdade para nomes e contagem ──
    verde, amarelo_names, vermelho_names = parse_resumo_table(content)

    print(f"  🟢 {len(verde)} verde(s): {verde}", flush=True)
    print(f"  🟡 {len(amarelo_names)} amarelo(s): {amarelo_names}", flush=True)
    print(f"  🔴 {len(vermelho_names)} vermelho(s): {vermelho_names}", flush=True)
    print(f"  📊 Total: {len(verde)+len(amarelo_names)+len(vermelho_names)} clientes", flush=True)

    # ── Para cada alerta, busca detalhes no markdown por nome ──
    all_alert_clients = []

    for nm in vermelho_names:
        print(f"  🔴 Processando: {nm}", flush=True)
        client = find_section_content(content, nm)
        client["nome"]    = nm
        client["status"]  = "red"
        client["manager"] = name
        client["insight"] = generate_insight(client)
        all_alert_clients.append(client)

    for nm in amarelo_names:
        print(f"  🟡 Processando: {nm}", flush=True)
        client = find_section_content(content, nm)
        client["nome"]    = nm
        client["status"]  = "yellow"
        client["manager"] = name
        client["insight"] = generate_insight(client)
        all_alert_clients.append(client)

    return {
        "name":    name,
        "week":    page_name,
        "verde":   verde,
        "clients": all_alert_clients,
    }


# ── HTML GENERATOR ───────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Scale — Saúde da Carteira</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:#080808;--surface:#111111;--surface2:#181818;--border:#232323;--border2:#2e2e2e;
      --text:#f0f0f0;--muted:#6b6b6b;--dim:#3a3a3a;
      --green:#22c55e;--green-bg:#061510;--green-border:#14532d;
      --yellow:#f59e0b;--yellow-bg:#120d00;--yellow-border:#78350f;
      --red:#ef4444;--red-bg:#120404;--red-border:#7f1d1d;
      --ai-bg:#0d0d1a;--ai-border:#2a2850;--ai-text:#a5b4fc;
    }}
    html{{scroll-behavior:smooth;}}
    body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;line-height:1.6;}}
    .header{{border-bottom:1px solid var(--border);padding:24px 40px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:rgba(8,8,8,.92);backdrop-filter:blur(12px);z-index:100;}}
    .logo{{font-size:26px;font-weight:900;letter-spacing:-.5px;color:#fff;text-transform:uppercase;}}
    .logo span{{display:inline-block;border:2px solid #fff;padding:1px 10px 1px 8px;margin-right:10px;font-size:22px;}}
    .header-meta{{text-align:right;}}
    .week-badge{{display:inline-block;background:var(--surface2);border:1px solid var(--border2);padding:4px 12px;border-radius:20px;font-size:12px;font-weight:500;color:var(--muted);margin-bottom:4px;}}
    .updated-at{{font-size:11px;color:var(--dim);}}
    main{{max-width:1400px;margin:0 auto;padding:40px 40px 80px;}}
    .section-title{{font-size:11px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:20px;padding-bottom:10px;border-bottom:1px solid var(--border);}}
    .macro-grid{{display:grid;grid-template-columns:1fr 1fr 1fr 280px;gap:16px;margin-bottom:48px;}}
    .status-card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:28px 24px;cursor:pointer;transition:border-color .2s,transform .15s;}}
    .status-card:hover{{transform:translateY(-2px);}}
    .status-card.green{{border-color:var(--green-border);background:var(--green-bg);}}
    .status-card.yellow{{border-color:var(--yellow-border);background:var(--yellow-bg);}}
    .status-card.red{{border-color:var(--red-border);background:var(--red-bg);}}
    .card-label{{font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:16px;display:flex;align-items:center;gap:8px;}}
    .card-label .dot{{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0;}}
    .green .card-label{{color:var(--green);}} .yellow .card-label{{color:var(--yellow);}} .red .card-label{{color:var(--red);}}
    .green .dot{{background:var(--green);box-shadow:0 0 8px var(--green);}}
    .yellow .dot{{background:var(--yellow);box-shadow:0 0 8px var(--yellow);}}
    .red .dot{{background:var(--red);box-shadow:0 0 8px var(--red);animation:pulse 2s infinite;}}
    @keyframes pulse{{0%,100%{{box-shadow:0 0 6px var(--red);}}50%{{box-shadow:0 0 14px var(--red);}}}}
    .card-number{{font-size:52px;font-weight:800;line-height:1;margin-bottom:6px;letter-spacing:-2px;}}
    .green .card-number{{color:var(--green);}} .yellow .card-number{{color:var(--yellow);}} .red .card-number{{color:var(--red);}}
    .card-pct{{font-size:13px;font-weight:500;color:var(--muted);}}
    .card-clients-label{{font-size:12px;color:var(--muted);margin-top:4px;}}
    .score-card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:28px 24px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;}}
    .score-ring-wrap{{position:relative;width:110px;height:110px;}}
    .score-ring-wrap svg{{transform:rotate(-90deg);}}
    .score-value{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:28px;font-weight:800;color:#fff;letter-spacing:-1px;}}
    .score-label{{font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);text-align:center;}}
    .health-bar{{width:100%;height:6px;border-radius:3px;background:var(--surface2);overflow:hidden;display:flex;margin-top:4px;}}
    .health-bar .seg-g{{background:var(--green);}} .health-bar .seg-y{{background:var(--yellow);}} .health-bar .seg-r{{background:var(--red);}}
    .manager-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:48px;}}
    .manager-card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 18px;cursor:pointer;transition:border-color .2s,transform .15s;}}
    .manager-card:hover,.manager-card.active{{border-color:var(--border2);transform:translateY(-1px);}}
    .manager-card.active{{border-color:#fff;}}
    .manager-name{{font-size:15px;font-weight:700;margin-bottom:14px;color:#fff;}}
    .manager-stats{{display:flex;gap:10px;margin-bottom:14px;}}
    .mgr-stat{{display:flex;flex-direction:column;gap:2px;}}
    .mgr-stat .n{{font-size:20px;font-weight:700;line-height:1;}}
    .mgr-stat .l{{font-size:10px;font-weight:500;letter-spacing:.5px;color:var(--muted);}}
    .n.g{{color:var(--green);}} .n.y{{color:var(--yellow);}} .n.r{{color:var(--red);}}
    .mgr-bar{{height:4px;border-radius:2px;background:var(--surface2);overflow:hidden;display:flex;}}
    .mgr-score-pill{{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;margin-top:10px;}}
    .score-high{{background:var(--green-bg);color:var(--green);border:1px solid var(--green-border);}}
    .score-mid{{background:var(--yellow-bg);color:var(--yellow);border:1px solid var(--yellow-border);}}
    .score-low{{background:var(--red-bg);color:var(--red);border:1px solid var(--red-border);}}
    .manager-total{{font-size:10px;color:var(--muted);margin-top:6px;}}
    .filter-bar{{display:flex;gap:8px;margin-bottom:24px;flex-wrap:wrap;align-items:center;}}
    .filter-btn{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 14px;font-size:12px;font-weight:500;color:var(--muted);cursor:pointer;transition:all .15s;font-family:'Inter',sans-serif;}}
    .filter-btn:hover{{border-color:var(--border2);color:var(--text);}}
    .filter-btn.active{{background:#fff;color:#000;border-color:#fff;}}
    .filter-separator{{width:1px;height:28px;background:var(--border);}}
    .cards-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(600px,1fr));gap:16px;}}
    .client-card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden;transition:border-color .2s,transform .15s;}}
    .client-card:hover{{border-color:var(--border2);transform:translateY(-1px);}}
    .client-card.red{{border-left:3px solid var(--red);}} .client-card.yellow{{border-left:3px solid var(--yellow);}}
    .card-header{{padding:18px 20px 16px;display:flex;align-items:flex-start;justify-content:space-between;gap:12px;border-bottom:1px solid var(--border);cursor:pointer;user-select:none;}}
    .card-header-left{{flex:1;}}
    .card-status-row{{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;}}
    .status-pill{{display:inline-flex;align-items:center;gap:5px;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;padding:3px 10px;border-radius:20px;}}
    .status-pill.red{{background:var(--red-bg);color:var(--red);border:1px solid var(--red-border);}}
    .status-pill.yellow{{background:var(--yellow-bg);color:var(--yellow);border:1px solid var(--yellow-border);}}
    .manager-pill{{font-size:10px;font-weight:500;color:var(--muted);background:var(--surface2);padding:3px 10px;border-radius:20px;border:1px solid var(--border);}}
    .funil-pill{{font-size:10px;font-weight:500;color:var(--dim);background:var(--surface2);padding:3px 10px;border-radius:20px;border:1px solid var(--border);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
    .client-name{{font-size:18px;font-weight:700;color:#fff;line-height:1.2;}}
    .card-chevron{{color:var(--muted);font-size:18px;flex-shrink:0;transition:transform .2s;margin-top:4px;}}
    .card-chevron.open{{transform:rotate(90deg);}}
    .card-body{{padding:0 20px;max-height:0;overflow:hidden;transition:max-height .35s ease,padding .2s;}}
    .card-body.open{{max-height:2000px;padding:20px;}}
    .info-block{{margin-bottom:16px;}}
    .info-label{{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:5px;}}
    .info-text{{font-size:13px;color:var(--text);line-height:1.65;}}
    .insight-block{{background:var(--ai-bg);border:1px solid var(--ai-border);border-radius:10px;padding:16px 18px;margin-top:4px;}}
    .insight-header{{display:flex;align-items:center;gap:8px;margin-bottom:10px;}}
    .insight-badge{{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--ai-text);background:rgba(99,102,241,.15);border:1px solid var(--ai-border);padding:2px 10px;border-radius:10px;}}
    .insight-text{{font-size:13px;color:#c7d2fe;line-height:1.7;}}
    .empty-state{{text-align:center;padding:60px 20px;color:var(--muted);grid-column:1/-1;}}
    .empty-state .big{{font-size:40px;margin-bottom:12px;}}
    footer{{border-top:1px solid var(--border);padding:20px 40px;display:flex;align-items:center;justify-content:space-between;}}
    .footer-logo{{font-size:14px;font-weight:800;color:var(--dim);letter-spacing:1px;}}
    .footer-info{{font-size:11px;color:var(--muted);text-align:right;}}
    @media(max-width:1100px){{.macro-grid{{grid-template-columns:1fr 1fr;}}.manager-grid{{grid-template-columns:repeat(3,1fr);}}.cards-grid{{grid-template-columns:1fr;}}}}
    @media(max-width:700px){{main{{padding:24px 16px 60px;}}.header{{padding:16px 20px;}}.macro-grid{{grid-template-columns:1fr;}}.manager-grid{{grid-template-columns:repeat(2,1fr);}}.logo{{font-size:20px;}}}}
  </style>
</head>
<body>
<header class="header">
  <div class="logo"><span>S</span>CALE</div>
  <div class="header-meta">
    <div class="week-badge" id="weekBadge">Semana —</div>
    <div class="updated-at" id="updatedAt">Carregando...</div>
  </div>
</header>
<main>
  <div class="section-title">Visão Macro — Saúde da Agência</div>
  <div class="macro-grid">
    <div class="status-card green" id="cardGreen" onclick="filterStatus('green')">
      <div class="card-label"><span class="dot"></span>Saudáveis</div>
      <div class="card-number" id="numGreen">—</div>
      <div class="card-pct" id="pctGreen">—%</div>
      <div class="card-clients-label">clientes</div>
    </div>
    <div class="status-card yellow" id="cardYellow" onclick="filterStatus('yellow')">
      <div class="card-label"><span class="dot"></span>Ponto de Atenção</div>
      <div class="card-number" id="numYellow">—</div>
      <div class="card-pct" id="pctYellow">—%</div>
      <div class="card-clients-label">clientes</div>
    </div>
    <div class="status-card red" id="cardRed" onclick="filterStatus('red')">
      <div class="card-label"><span class="dot"></span>Críticos</div>
      <div class="card-number" id="numRed">—</div>
      <div class="card-pct" id="pctRed">—%</div>
      <div class="card-clients-label">clientes</div>
    </div>
    <div class="score-card">
      <div class="score-ring-wrap">
        <svg width="110" height="110" viewBox="0 0 110 110">
          <circle cx="55" cy="55" r="46" fill="none" stroke="#1a1a1a" stroke-width="10"/>
          <circle id="scoreCircle" cx="55" cy="55" r="46" fill="none" stroke="#22c55e"
            stroke-width="10" stroke-linecap="round" stroke-dasharray="289" stroke-dashoffset="289"/>
        </svg>
        <div class="score-value" id="scoreValue">—</div>
      </div>
      <div class="score-label">Score de Saúde</div>
      <div class="health-bar" id="globalHealthBar" style="margin-top:8px;"></div>
    </div>
  </div>
  <div class="section-title">Visão por Gestor</div>
  <div class="manager-grid" id="managerGrid"></div>
  <div class="filter-bar">
    <button class="filter-btn active" id="btnAll" onclick="filterStatus('all')">Todos os alertas</button>
    <button class="filter-btn" id="btnYellow" onclick="filterStatus('yellow')">🟡 Ponto de atenção</button>
    <button class="filter-btn" id="btnRed" onclick="filterStatus('red')">🔴 Críticos</button>
    <div class="filter-separator"></div>
    <span id="managerFilterBtns"></span>
  </div>
  <div class="section-title">Clientes que precisam de ação</div>
  <div class="cards-grid" id="cardsGrid"></div>
</main>
<footer>
  <div class="footer-logo">SCALE</div>
  <div class="footer-info">Atualiza toda quarta-feira às 05h00 BRT<br>Dados: ClickUp War Rooms</div>
</footer>
<script>
const DATA = {DATA_JSON};
(function(){{
  let tg=0,ty=0,tr=0;
  DATA.managers.forEach(m=>{{
    tg+=m.verde.length;
    ty+=m.clients.filter(c=>c.status==='yellow').length;
    tr+=m.clients.filter(c=>c.status==='red').length;
  }});
  const tot=tg+ty+tr;
  const score=Math.round((tg*100+ty*50)/tot);
  document.getElementById('weekBadge').textContent='Semana '+DATA.week;
  document.getElementById('updatedAt').textContent='Atualizado em '+DATA.updatedAt;
  document.getElementById('numGreen').textContent=tg;
  document.getElementById('numYellow').textContent=ty;
  document.getElementById('numRed').textContent=tr;
  document.getElementById('pctGreen').textContent=((tg/tot)*100).toFixed(1)+'%';
  document.getElementById('pctYellow').textContent=((ty/tot)*100).toFixed(1)+'%';
  document.getElementById('pctRed').textContent=((tr/tot)*100).toFixed(1)+'%';
  document.getElementById('scoreValue').textContent=score;
  const circ=2*Math.PI*46;
  const offset=circ-(score/100)*circ;
  const ringColor=score>=70?'#22c55e':score>=50?'#f59e0b':'#ef4444';
  const sc=document.getElementById('scoreCircle');
  sc.style.strokeDasharray=circ;
  sc.style.stroke=ringColor;
  sc.style.strokeDashoffset=circ;
  setTimeout(()=>{{sc.style.transition='stroke-dashoffset 1.2s ease';sc.style.strokeDashoffset=offset;}},200);
  const ghb=document.getElementById('globalHealthBar');
  ghb.innerHTML=`<div class="seg-g" style="width:${{(tg/tot*100).toFixed(1)}}%"></div><div class="seg-y" style="width:${{(ty/tot*100).toFixed(1)}}%"></div><div class="seg-r" style="width:${{(tr/tot*100).toFixed(1)}}%"></div>`;
  const mgGrid=document.getElementById('managerGrid');
  DATA.managers.forEach(m=>{{
    const v=m.verde.length;
    const y=m.clients.filter(c=>c.status==='yellow').length;
    const r=m.clients.filter(c=>c.status==='red').length;
    const t=v+y+r;
    const ms=t>0?Math.round((v*100+y*50)/t):0;
    const sc2=ms>=70?'score-high':ms>=50?'score-mid':'score-low';
    const card=document.createElement('div');
    card.className='manager-card';
    card.dataset.manager=m.name;
    card.innerHTML=`<div class="manager-name">${{m.name}}</div><div class="manager-stats"><div class="mgr-stat"><div class="n g">${{v}}</div><div class="l">Saudáveis</div></div><div class="mgr-stat"><div class="n y">${{y}}</div><div class="l">Atenção</div></div><div class="mgr-stat"><div class="n r">${{r}}</div><div class="l">Críticos</div></div></div><div class="mgr-bar"><div class="seg-g" style="width:${{t>0?(v/t*100).toFixed(0):0}}%"></div><div class="seg-y" style="width:${{t>0?(y/t*100).toFixed(0):0}}%"></div><div class="seg-r" style="width:${{t>0?(r/t*100).toFixed(0):0}}%"></div></div><div class="mgr-score-pill ${{sc2}}">Score ${{ms}}/100</div><div class="manager-total">${{t}} clientes no total</div>`;
    card.addEventListener('click',()=>filterManager(m.name,card));
    mgGrid.appendChild(card);
  }});
  const mfb=document.getElementById('managerFilterBtns');
  DATA.managers.forEach(m=>{{
    const btn=document.createElement('button');
    btn.className='filter-btn';
    btn.dataset.manager=m.name;
    btn.textContent=m.name;
    btn.addEventListener('click',()=>filterManager(m.name,btn));
    mfb.appendChild(btn);
  }});
  const allAlerts=[];
  DATA.managers.forEach(m=>{{
    m.clients.filter(c=>c.status==='red').forEach(c=>allAlerts.push({{...c,manager:m.name}}));
    m.clients.filter(c=>c.status==='yellow').forEach(c=>allAlerts.push({{...c,manager:m.name}}));
  }});
  window._allAlerts=allAlerts;
  window._statusFilter='all';
  window._managerFilter=null;
  renderCards();
}})();
function renderCards(){{
  const grid=document.getElementById('cardsGrid');
  grid.innerHTML='';
  const sf=window._statusFilter,mf=window._managerFilter;
  let filtered=window._allAlerts;
  if(sf==='red') filtered=filtered.filter(c=>c.status==='red');
  if(sf==='yellow') filtered=filtered.filter(c=>c.status==='yellow');
  if(mf) filtered=filtered.filter(c=>c.manager===mf);
  if(filtered.length===0){{
    grid.innerHTML='<div class="empty-state"><div class="big">✦</div><p>Nenhum cliente para este filtro</p></div>';
    return;
  }}
  filtered.forEach((c,idx)=>{{
    const card=document.createElement('div');
    card.className='client-card '+c.status;
    const sl=c.status==='red'?'🔴 Crítico':'🟡 Atenção';
    card.innerHTML=`<div class="card-header" onclick="toggleCard(this)"><div class="card-header-left"><div class="card-status-row"><span class="status-pill ${{c.status}}">${{sl}}</span><span class="manager-pill">${{c.manager}}</span><span class="funil-pill" title="${{c.funil}}">${{c.funil}}</span></div><div class="client-name">${{c.nome}}</div></div><div class="card-chevron ${{idx<3?'open':''}}">›</div></div><div class="card-body ${{idx<3?'open':''}}"><div class="info-block"><div class="info-label">Problema</div><div class="info-text">${{c.problema}}</div></div><div class="info-block"><div class="info-label">O que os dados mostram</div><div class="info-text">${{c.dados}}</div></div><div class="info-block"><div class="info-label">O que já foi tentado</div><div class="info-text">${{c.tentou}}</div></div><div class="info-block"><div class="info-label">Sugestão do gestor</div><div class="info-text">${{c.sugestao}}</div></div><div class="insight-block"><div class="insight-header"><span class="insight-badge">✦ Insight Claude</span></div><div class="insight-text">${{c.insight}}</div></div></div>`;
    grid.appendChild(card);
  }});
}}
function toggleCard(h){{
  const b=h.nextElementSibling,ch=h.querySelector('.card-chevron');
  b.classList.toggle('open');ch.classList.toggle('open');
}}
let _am=null;
function filterManager(name,el){{
  const same=window._managerFilter===name;
  window._managerFilter=same?null:name;
  _am=same?null:name;
  document.querySelectorAll('.manager-card').forEach(c=>c.classList.toggle('active',c.dataset.manager===_am));
  document.querySelectorAll('[data-manager]').forEach(b=>b.classList.toggle('active',b.dataset.manager===_am));
  renderCards();
}}
function filterStatus(s){{
  window._statusFilter=s;
  document.getElementById('btnAll').classList.toggle('active',s==='all');
  document.getElementById('btnYellow').classList.toggle('active',s==='yellow');
  document.getElementById('btnRed').classList.toggle('active',s==='red');
  if(s==='green'){{
    document.getElementById('cardsGrid').innerHTML='<div class="empty-state"><div class="big">🟢</div><p>Clientes saudáveis — nenhuma ação necessária</p></div>';
    return;
  }}
  renderCards();
}}
</script>
</body>
</html>"""


def build_html(managers_data: list, week_str: str) -> str:
    today = datetime.date.today().strftime("%d/%m/%Y")

    js_managers = []
    for m in managers_data:
        js_managers.append({
            "name":    m["name"],
            "verde":   m["verde"],
            "clients": [
                {
                    "status":   c["status"],
                    "manager":  c["manager"],
                    "nome":     c["nome"],
                    "funil":    c.get("funil", "—"),
                    "problema": c.get("problema", "—"),
                    "dados":    c.get("dados", "—"),
                    "tentou":   c.get("tentou", "—"),
                    "sugestao": c.get("sugestao", "—"),
                    "insight":  c.get("insight", "—"),
                }
                for c in m["clients"]
            ]
        })

    data_obj = {
        "week":      week_str,
        "updatedAt": today,
        "managers":  js_managers,
    }

    data_json = json.dumps(data_obj, ensure_ascii=False, indent=2)
    # 1) Injeta o JSON
    html = HTML_TEMPLATE.replace("{DATA_JSON}", data_json)
    # 2) Converte {{ e }} do template para { e } no HTML final
    html = html.replace("{{", "{").replace("}}", "}")
    return html


# ── ENTRY POINT ──────────────────────────────────────────────────────────────

def main():
    if not CLICKUP_TOKEN:
        print("❌ CLICKUP_API_TOKEN não encontrada.", flush=True)
        sys.exit(1)

    print(f"🚀 Scale Dashboard Generator — {datetime.date.today()}", flush=True)

    managers_data = []
    week_str = "—"
    week_detected = False

    for mgr in MANAGERS:
        try:
            data = fetch_manager_data(mgr)
            managers_data.append(data)
            if not week_detected and data.get("week"):
                w = re.sub(r".*Semana\s*", "", data["week"]).strip()
                week_str = w
                week_detected = True
        except Exception as e:
            print(f"  ❌ Erro ao processar {mgr['name']}: {e}", flush=True)
            import traceback; traceback.print_exc()
            managers_data.append({"name": mgr["name"], "week": None, "verde": [], "clients": []})

    print(f"\n✅ Todos os gestores processados.", flush=True)
    print(f"📅 Semana: {week_str}", flush=True)

    html = build_html(managers_data, week_str)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"💾 Salvo em: {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
