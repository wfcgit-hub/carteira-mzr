"""Gera a versão pública do painel: um único index.html que roda fora do claude.ai, com os dados dentro.

Uso: python -I -X utf8 site_estatico.py <pasta do painel> <pasta de saída>
Lê a página do painel (site/index.html, a mesma do claude.ai) e os quatro arquivos de dados (dados/*.json) e escreve
<saída>/index.html. Um "dash" mínimo imita o que o claude.ai oferece à página (dados, desenhos, cálculo e cores), para
que o painel público e o do claude.ai sejam o mesmo código. Sem rede além do d3 (cdnjs) e das fontes (Google Fonts).
"""
import datetime as dt
import json
import sys
from pathlib import Path

PASTA, SAIDA = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
pagina = (PASTA / 'site' / 'index.html').read_text('utf-8').replace('<meta name="dash-one-writer">', '')
dados = {n: json.loads((PASTA / 'dados' / f'{n}.json').read_text('utf-8'))['rows'] for n in ('serie', 'classes', 'monitor', 'termo', 'fontes')
         if (PASTA / 'dados' / f'{n}.json').exists()}
ultimo = dados['serie'][-1]['data']
agora = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).strftime('%d/%m/%Y %H:%M')
dados_js = json.dumps(dados, ensure_ascii=False).replace('</', '<\\/')

CABECA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Carteira MZR</title>
<meta name="description" content="Painel diário da carteira do case MZR Family Office contra a régua e os principais índices.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>
  /* paleta do deck do case: azul-noite, azul e ouro; o ouro marca a carteira */
  :root {
    color-scheme: light;
    --color-bg: #FBFAF7; --color-panel: #FFFFFF; --color-fg: #14213A; --color-fg-muted: #5B6779; --color-border-line: #E3DED4;
    --color-ok: #2E7D5B; --color-warn: #A86B12; --color-bad: #B3402E;
    --serie-0: #B08C58; --serie-1: #1D3557; --serie-2: #8A8478; --serie-3: #2E73AE; --serie-4: #7BAED8; --serie-5: #4F7F6A; --serie-6: #8C5E9E;
    --cds-chart-grid: #EEEAE2; --cds-chart-axis: #C9C2B5; --cds-chart-reference: #9C9486; --cds-text-secondary: #5B6779;
    --cds-surface-popover: #FFFFFF; --cds-border: #E3DED4; --cds-shadow-popover: 0 6px 24px rgba(20,33,58,.12);
    --font-anthropic-sans: 'Montserrat', system-ui, -apple-system, 'Segoe UI', sans-serif;
    --font-anthropic-serif: 'Source Serif 4', Georgia, 'Times New Roman', serif;
    --cds-font-size-caption: 12px; --cds-font-size-body: 14px; --cds-font-size-heading: 18px; --cds-font-size-title: 34px;
    --cds-font-weight-medium: 600;
    --cds-gap-xs: 4px; --cds-gap-sm: 8px; --cds-gap-md: 14px; --cds-gap-lg: 20px; --cds-gap-xl: 32px;
    --cds-pad-sm: 8px; --cds-pad-md: 12px; --cds-pad-lg: 18px; --cds-radius: 10px;
  }
  @media (prefers-color-scheme: dark) { :root {
    color-scheme: dark;
    --color-bg: #0A1727; --color-panel: #12213A; --color-fg: #EEF2F7; --color-fg-muted: #A9B8CA; --color-border-line: #263A57;
    --color-ok: #5FBF91; --color-warn: #E0A84B; --color-bad: #E57A66;
    --serie-0: #D2AE73; --serie-1: #8FB3E0; --serie-2: #A49E92; --serie-3: #5E9CDB; --serie-4: #B5D3EE; --serie-5: #7FB79C; --serie-6: #C39AD6;
    --cds-chart-grid: #1B2D47; --cds-chart-axis: #3A4E6A; --cds-chart-reference: #6F829C; --cds-text-secondary: #A9B8CA;
    --cds-surface-popover: #14263F; --cds-border: #2C4262; --cds-shadow-popover: 0 6px 24px rgba(0,0,0,.4);
  } }
  html, body { margin: 0; background: var(--color-bg); color: var(--color-fg); font-family: var(--font-anthropic-sans); font-size: 14px; }
  .publico { max-width: 1120px; margin: 0 auto; padding-block: 28px 40px;
             padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
  .publico-rodape { margin-top: 32px; padding-top: 14px; border-top: 1px solid var(--color-border-line); color: var(--color-fg-muted); font-size: 12px; line-height: 1.6; }
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
/* um "dash" mínimo: o mesmo contrato que a página usa no claude.ai */
(function () {
  const DADOS = __DADOS__;
  const fontes = {}; Object.keys(DADOS).forEach((k) => { fontes[k] = { status: 'ok', data: DADOS[k], message: '', refreshing: false, truncated: false, meta: [] }; });
  const calcs = {}, desenhos = {}, ouvintes = [];
  const valor = (id) => {
    if (fontes[id]) return fontes[id];
    const c = calcs[id];
    if (!c) return { status: 'missing', data: [] };
    if (!c.cache) c.cache = { status: 'ok', data: c.fn(...c.inputs.map((i) => valor(i).data)) };
    return c.cache;
  };
  const rodar = (nome) => {
    const fn = desenhos[nome]; if (!fn) return;
    document.querySelectorAll(`[data-draw="${nome}"]`).forEach((el) => {
      if (el.closest('[hidden]')) return;
      try { fn(el); } catch (e) { console.error(nome, e); }
    });
  };
  const tudo = () => { ouvintes.forEach((f) => f()); Object.keys(desenhos).forEach(rodar); Object.keys(desenhos).forEach(rodar); };
  window.dash = {
    colors: [0, 1, 2, 3, 4, 5, 6].map((i) => `var(--serie-${i})`),
    data: valor,
    calc: (id, o) => { calcs[id] = { inputs: o.inputs, fn: o.fn, cache: null }; },
    loader: () => {},
    onData: (f) => { ouvintes.push(f); },
    draw: (nome, fn) => { if (fn) { desenhos[nome] = typeof fn === 'function' ? fn : fn.update || fn.mount; } else rodar(nome); },
    params: () => ({}), setParams: () => {}, resetParams: () => {}, setLink: () => {}, refresh: () => {},
  };
  document.addEventListener('DOMContentLoaded', tudo);
  let t; window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(tudo, 150); });
})();
</script>
</head>
<body>
<main class="publico" id="dash-root">
"""
RODAPE = f"""
<footer class="publico-rodape">
  Atualizado em {agora} (Brasília), com dados até {dt.date.fromisoformat(ultimo):%d/%m/%Y}. Fontes públicas: Banco Central (CDI e PTAX), CVM (cotas diárias de fundos passivos, no lugar dos índices),
  Tesouro Direto e FRED (juro dos Treasuries; este painel não é endossado nem certificado pelo Federal Reserve Bank of St. Louis). Case MZR Family Office, Summer 2026/2.
  Antes de 13/11/2026, simulação com os pesos de hoje, informação suplementar; não é desempenho passado. Rentabilidade passada não é garantia de rentabilidade futura; a rentabilidade divulgada não é líquida de impostos.
</footer>
</main>
</body>
</html>
"""
SAIDA.mkdir(parents=True, exist_ok=True)
(SAIDA / 'index.html').write_text(CABECA.replace('__DADOS__', dados_js) + pagina + RODAPE, 'utf-8')
(SAIDA / '.nojekyll').write_text('', 'utf-8')
print('site gerado:', SAIDA / 'index.html', f'(dados até {ultimo})')
