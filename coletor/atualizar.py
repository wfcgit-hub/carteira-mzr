"""Painel da carteira MZR: coleta os dados públicos, calcula carteira, régua e índices e grava os arquivos do painel.

Uso: python -I -X utf8 atualizar.py <pasta do painel>
     (-I: os arquivos baixados são dados de terceiros; o script não importa nada da pasta deles)

Fontes, todas públicas e automáticas (nada da B3, da Anbima, do MSCI, da CME, da ICE ou do Yahoo):
- Banco Central, SGS: CDI diário (série 12) e dólar PTAX de venda (série 1);
- FRED (Fed de St. Louis): juro dos Treasuries de 2 e de 10 anos (DGS2, DGS10), séries do governo americano. Desde 21/11/2026
  a bolsa global não vem mais do FRED (o S&P 500 lá tem copyright da S&P Dow Jones Indices): vem da cota de um fundo na CVM;
- CVM, dados abertos: cotas diárias de fundos passivos, que fazem as vezes dos índices da B3 e da Anbima
  (cotas líquidas de taxa de administração; ver FUNDOS);
- Tesouro Transparente: preços e taxas do Tesouro Direto, para o juro de 1 ano a termo (LTNs de ~1 e ~2 anos).

A carteira e a régua são as do case (slides 9 e 10), antes do IR, rebalanceadas no 1º dia útil de cada mês.
Até 13/11/2026, a data de corte do case, a carteira não existia: o trecho é uma simulação com os pesos de hoje.
Dali em diante, o painel acompanha a carteira de fato.
"""
import csv
import statistics as st
import urllib.parse
import datetime as dt
import io
import json
import math
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

PASTA = Path(sys.argv[1]).resolve()
BRUTOS, DADOS = PASTA / 'dados-brutos', PASTA / 'dados'
INICIO = dt.date(2024, 11, 13)          # 24 meses de simulação
ATIVA = dt.date(2026, 11, 13)           # data de corte do case: a carteira passa a existir
BRT = dt.timezone(dt.timedelta(hours=-3))
HOJE = dt.datetime.now(BRT).date()

FUNDOS = {  # classe: (CNPJ, nome do fundo) — fundos de investimento que reportam cota diária à CVM (os ETFs não reportam)
    'ibov': ('24546223000117', 'Itaú Vértice Ibovespa Equities FIF Ações'),
    'imab': ('10740658000193', 'Caixa Brasil IMA-B Títulos Públicos FIF RF'),
    'imab5': ('11060913000110', 'Caixa Brasil IMA-B 5 Títulos Públicos FIF RF'),
    'imab5mais': ('13327340000173', 'BB Previdenciário RF IMA-B 5+ Títulos Públicos FIF'),
    'irfm': ('14508605000100', 'Caixa Brasil IRF-M Títulos Públicos FIF RF'),
    'credito': ('07096435000120', 'Itaú High Grade RF Crédito Privado FIF'),
    'global': ('31874833000105', 'Trend S&P 500 Dólar FIA (XP Asset)'),  # replica o S&P 500 sem proteção cambial
}
FERIADOS = {dt.date(*d) for d in (
    (2024, 11, 15), (2024, 11, 20), (2024, 12, 25), (2025, 1, 1), (2025, 3, 3), (2025, 3, 4), (2025, 4, 18), (2025, 4, 21),
    (2025, 5, 1), (2025, 6, 19), (2025, 11, 20), (2025, 12, 25), (2026, 1, 1), (2026, 2, 16), (2026, 2, 17), (2026, 4, 3),
    (2026, 4, 21), (2026, 5, 1), (2026, 6, 4), (2026, 9, 7), (2026, 10, 12), (2026, 11, 2), (2026, 11, 20), (2026, 12, 25),
    (2027, 1, 1), (2027, 2, 8), (2027, 2, 9), (2027, 3, 26), (2027, 4, 21), (2027, 5, 27), (2027, 9, 7), (2027, 10, 12),
    (2027, 11, 2), (2027, 11, 15), (2027, 11, 20), (2027, 12, 25))}   # feriados bancários nacionais (sem os que caem no fim de semana)
LCI_CDI = 0.866   # LCI/LCA de banco grande: 86,6% do CDI (Quantum, via Seu Dinheiro, 24/09/2026)
# classe: (peso na carteira, peso na régua, como a série é feita)
PESOS = {  # carteira J do case (16/11/2026)
    'LCI/LCA': (30.0, 30.0, '86,6% do CDI (BCB, série 12)'),
    'Tesouro Selic': (12.5, 20.0, 'CDI (BCB, série 12)'),
    'Crédito high grade': (12.5, 10.0, 'cota do ' + FUNDOS['credito'][1]),
    'Prefixado 2 anos': (20.0, 7.5, 'cota do ' + FUNDOS['irfm'][1]),
    'IPCA+ 3 anos': (7.5, 7.5, 'cota do ' + FUNDOS['imab5'][1]),
    'IPCA+ 10 anos': (0.0, 7.5, 'cota do ' + FUNDOS['imab5mais'][1]),
    'Bolsa Brasil': (2.5, 5.0, 'cota do ' + FUNDOS['ibov'][1]),
    'Bolsa global sem hedge': (2.5, 7.5, 'cota do ' + FUNDOS['global'][1] + ', S&P 500 sem proteção cambial'),
    'Treasury 2 anos': (2.5, 5.0, 'Treasury de 2 anos pelo juro (FRED, DGS2), convertido pela PTAX (BCB)'),
    'Treasury 10 anos': (10.0, 0.0, 'Treasury de 10 anos pelo juro (FRED, DGS10), convertido pela PTAX (BCB)'),
}
assert abs(sum(p[0] for p in PESOS.values()) - 100) < 1e-9 and abs(sum(p[1] for p in PESOS.values()) - 100) < 1e-9
REALIZAR, SAIR = 14.0, 15.1   # termômetro dos cenários no juro de 2 anos: o base no preço (~14,0) e o piso da deterioração no preço (15,1); a carteira não vende no meio (candidato, 16/11)

# A tese do case (modelo de 16/11/2026: saidas/mzr-summer/slides/carteira.json e risco_carteira.json; documento 17).
# Fixa: muda só se o case mudar. Retornos de 12 meses, em reais, antes do IR, salvo os "_liq".
TESE = dict(
    cenarios=[
        dict(nome='Ajuste crível', chance=10, carteira=12.50, regua=13.49, cdi=12.9, carteira_liq=10.29, regua_liq=11.22, selic_liq=10.64,
             selic=12.0, ipca=4.6, dolar=5.10),
        dict(nome='Ajuste já apresentado', chance=60, carteira=13.53, regua=13.18, cdi=13.5, carteira_liq=11.26, regua_liq=11.01, selic_liq=11.14,
             selic=13.5, ipca=5.5, dolar=5.67),
        dict(nome='Deterioração', chance=30, carteira=15.88, regua=13.44, cdi=14.5, carteira_liq=13.23, regua_liq=11.18, selic_liq=11.96,
             selic=15.5, ipca=7.1, dolar=6.80),
    ],
    media=dict(carteira=14.13, regua=13.29, cdi=13.74, carteira_liq=11.75, regua_liq=11.08, selic_liq=11.34),
    perde_cdi_chance=24,
    mundos=['Mundo base', 'Inflação alta nos EUA', 'Aversão a risco'],
    comb_carteira=[[12.6, 11.8, 13.1], [13.6, 12.8, 14.2], [15.8, 14.9, 16.6]],
    comb_regua=[[14.0, 12.8, 11.4], [14.1, 12.8, 11.3], [14.9, 13.6, 11.9]],
    comb_chance=[[7.1, 1.9, 1.1], [32.1, 14.9, 12.9], [10.8, 8.2, 11.0]],
    classes=[
        dict(classe='Pós-fixado (LCI/LCA e Tesouro Selic)', regua=50.0, carteira=42.5, cen=[12.9, 13.5, 14.5], media=13.7,
             veiculo='LCI/LCA de 3 ou 4 bancos grandes (30%), em escada de 6 a 24 meses, e Tesouro Selic (12,5%)', liquidez='D+0 a D+1; LCI/LCA no vencimento',
             porque='Menos caixa, para abrir espaço aos desvios'),
        dict(classe='Crédito high grade', regua=10.0, carteira=12.5, cen=[14.9, 14.75, 12.9], media=14.2,
             veiculo='Fundo de crédito high grade', liquidez='D+30', porque='Rende 14,8% no cenário mais provável, acima do CDI'),
        dict(classe='Prefixado 2 anos', regua=7.5, carteira=20.0, cen=[17.11, 15.51, 13.37], media=15.0,
             veiculo='Tesouro Prefixado (LTN) de ~2 anos', liquidez='D+1', porque='Rende 15,5% no cenário mais provável, 2 pontos acima do CDI'),
        dict(classe='IPCA+ 3 anos', regua=7.5, carteira=7.5, cen=[16.52, 14.1, 11.41], media=13.5,
             veiculo='Tesouro IPCA+ curto e incentivadas AAA', liquidez='D+1', porque='Igual à régua'),
        dict(classe='IPCA+ 10 anos', regua=7.5, carteira=0.0, cen=[22.4, 14.92, 5.6], media=12.9,
             veiculo='Tesouro IPCA+ longo', liquidez='D+1', porque='Rende só 5,6% na deterioração'),
        dict(classe='Bolsa Brasil', regua=5.0, carteira=2.5, cen=[29.86, 12.0, -15.48], media=5.5,
             veiculo='Ações de bancos de qualidade e exportadoras', liquidez='D+2', porque='Cai 15,5% na deterioração'),
        dict(classe='Bolsa global sem hedge', regua=7.5, carteira=2.5, cen=[0.29, 7.54, 23.27], media=11.5,
             veiculo='Fundo de ações no exterior, sem hedge', liquidez='D+3 a D+5', porque='Rende 7,5% no cenário mais provável, abaixo do CDI'),
        dict(classe='Treasury 2 anos', regua=5.0, carteira=2.5, cen=[-3.27, 8.98, 32.98], media=15.0,
             veiculo='Treasury de 2 anos, sem hedge', liquidez='D+2', porque='A moeda forte foi para o Treasury de 10 anos'),
        dict(classe='Treasury 10 anos', regua=0.0, carteira=10.0, cen=[-1.74, 10.73, 35.56], media=16.9,
             veiculo='Treasury de 10 anos, sem hedge', liquidez='D+2', porque='O seguro da carteira: +35,6% na deterioração'),
    ],
    historia=dict(janela='dez/2016 a out/2026', meses=119, cdi_anual=9.3,
                  carteira=dict(retorno_anual=9.8, vol=1.8, perda_max=-0.9, pior_mes=-0.6),
                  regua=dict(retorno_anual=10.4, vol=2.0, perda_max=-1.7, pior_mes=-1.5),
                  episodios=[('Greve dos caminhoneiros (mai–jun/18)', 1.7, 1.0), ('Covid (fev–mar/20)', 2.4, -1.5), ('Fiscal 2021 (jul–nov/21)', 2.2, 2.0),
                             ('Juros globais 2022 (jan–out/22)', 5.1, 6.0), ('Fiscal 2024 (out–dez/24)', 2.1, 2.2), ('Eleição 2026 (out/26)', 1.1, 0.4)]),
    choque_2610=dict(carteira=-0.10, regua=-0.74),
)


# ------------------------------------------------------------------ coleta
def baixar(url, destino, sempre=False, tentativas=6):
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists() and not sempre:
        return destino
    for k in range(tentativas):
        try:
            # o FRED não responde a clientes que não se apresentam como curl (testado em 14/11/2026)
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
            with urllib.request.urlopen(req, timeout=180) as r:
                destino.write_bytes(r.read())
            return destino
        except Exception as erro:  # noqa: BLE001
            if k == tentativas - 1:
                # 21/11/2026: o Tesouro Transparente e o BCB às vezes derrubam a conexão o dia todo. Com cópia guardada
                # (cache do Actions), o painel segue com ela; o atraso aparece em dados/fontes.json e no aviso do topo.
                if destino.exists():
                    print(f'aviso: falhou {url} ({erro}); uso a cópia guardada')
                    return destino
                raise RuntimeError(f'falhou {url}: {erro}')
            time.sleep(10 * (k + 1))   # o BCB às vezes derruba a conexão (21/11/2026): espera mais a cada tentativa


def sgs(codigo):
    ini = (INICIO - dt.timedelta(days=10)).strftime('%d/%m/%Y')
    url = f'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados?formato=json&dataInicial={ini}&dataFinal={HOJE:%d/%m/%Y}'
    f = baixar(url, BRUTOS / 'bcb' / f'sgs{codigo}.json', sempre=True)
    return {dt.datetime.strptime(x['data'], '%d/%m/%Y').date(): float(x['valor']) for x in json.loads(f.read_text('utf-8'))}


def fred(serie):
    """Série diária do FRED. Em 17/11/2026 o FRED passou a entregar só o último ano do S&P 500; por isso o download novo
    é somado ao que já estava no cache (dados-brutos), sem apagar os dias antigos."""
    destino = BRUTOS / 'fred' / f'{serie}.csv'

    def ler(texto):
        out = {}
        for linha in list(csv.reader(io.StringIO(texto)))[1:]:
            if len(linha) == 2 and linha[1] not in ('', '.'):
                out[dt.date.fromisoformat(linha[0])] = float(linha[1])
        return out

    antigo = ler(destino.read_text('utf-8')) if destino.exists() else {}
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie}&cosd={INICIO - dt.timedelta(days=10):%Y-%m-%d}'
    novo = ler(baixar(url, destino, sempre=True).read_text('utf-8'))
    out = {**antigo, **novo}
    destino.write_text('observation_date,' + serie + '\n' + ''.join(f'{d.isoformat()},{v}\n' for d, v in sorted(out.items())), 'utf-8')
    return out


def meses():
    m = dt.date(INICIO.year, INICIO.month, 1)
    while m <= HOJE:
        yield m
        m = dt.date(m.year + (m.month == 12), m.month % 12 + 1, 1)


def cvm():
    alvo = {c: k for k, (c, _) in FUNDOS.items()}
    cotas = {k: {} for k in FUNDOS}
    lista = list(meses())
    for m in lista:
        nome = f'inf_diario_fi_{m:%Y%m}.zip'
        recente = m >= dt.date(lista[-1].year, lista[-1].month, 1) - dt.timedelta(days=31)  # este mês e o anterior: sempre
        f = baixar(f'https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/{nome}', BRUTOS / 'cvm-diario' / nome, sempre=recente)
        with zipfile.ZipFile(f) as z:
            for x in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding='latin-1'), delimiter=';'):
                c = ''.join(ch for ch in (x.get('CNPJ_FUNDO_CLASSE') or x.get('CNPJ_FUNDO') or '') if ch.isdigit())
                if c in alvo and not x.get('ID_SUBCLASSE'):
                    cotas[alvo[c]][dt.date.fromisoformat(x['DT_COMPTC'])] = float(x['VL_QUOTA'])
    return cotas


def tesouro():
    url = ('https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/'
           '796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv')
    f = baixar(url, BRUTOS / 'tesouro' / 'PrecoTaxaTesouroDireto.csv', sempre=True)
    num = lambda s: float(s.replace(',', '.')) if s.strip() else None  # noqa: E731
    por_dia = {}
    r = csv.reader(io.StringIO(f.read_bytes().decode('latin-1')), delimiter=';')
    next(r)
    reais = {}
    for l in r:
        if l[0] not in ('Tesouro Prefixado', 'Tesouro IPCA+'):
            continue
        base = dt.datetime.strptime(l[2], '%d/%m/%Y').date()
        if base < HOJE - dt.timedelta(days=800):
            continue
        venc = dt.datetime.strptime(l[1], '%d/%m/%Y').date()
        tc, tv = num(l[3]), num(l[4])
        taxa = (tc + tv) / 2 if (tc and tv) else (tc or tv)   # meio entre a compra e a venda do investidor
        if taxa:
            (por_dia if l[0] == 'Tesouro Prefixado' else reais).setdefault(base, []).append(((venc - base).days / 365.25, taxa))
    termo, pre2, inf3, real = {}, {}, {}, {}
    for d, pts in por_dia.items():
        pts.sort()

        def interp(t):
            for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
                if t0 <= t <= t1:
                    return r0 + (r1 - r0) * (t - t0) / (t1 - t0)
            return None
        r1, r2 = interp(1.0), interp(2.0)
        if r1 and r2:
            termo[d] = ((1 + r2 / 100) ** 2 / (1 + r1 / 100) - 1) * 100
        if r2:
            pre2[d] = r2
        # inflação implícita de 3 anos: prefixado (LTN) contra IPCA+ (NTN-B Principal) interpolados no mesmo prazo
        rr = sorted(reais.get(d, []))
        p3 = interp(3.0)
        q3 = next((a0 + (a1 - a0) * (3.0 - t0) / (t1 - t0) for (t0, a0), (t1, a1) in zip(rr, rr[1:]) if t0 <= 3.0 <= t1), None)
        if p3 and q3:
            inf3[d] = ((1 + p3 / 100) / (1 + q3 / 100) - 1) * 100
        q10 = next((a0 + (a1 - a0) * (10.0 - t0) / (t1 - t0) for (t0, a0), (t1, a1) in zip(rr, rr[1:]) if t0 <= 10.0 <= t1), None)
        if q3 and q10:
            real[d] = (q3, q10)
    return termo, pre2, inf3, real


# ------------------------------------------------------------------ cálculo
def titulo(a, b, dias, anos):
    """Retorno de um Treasury de prazo constante entre dois dias, pelo juro (%): carrego menos duração × variação,
    com convexidade; título ao par com cupom semestral (a mesma conta do bloco 11 do case, em base diária)."""
    a, b = a / 100, b / 100
    n = 2 * anos
    dur = (1 - (1 + a / 2) ** -n) / (a / 2) / 2 / (1 + a / 2)
    return a * dias / 365 - dur * (b - a) + 0.5 * dur ** 2 * (b - a) ** 2


def focus(indicadores, ano, dias=200):
    """Focus (BCB, Olinda): mediana, mínimo, máximo e respondentes das expectativas anuais, base de cálculo 0."""
    filtro = '(' + ' or '.join(f"Indicador eq '{i}'" for i in indicadores) + f") and DataReferencia eq '{ano}' and baseCalculo eq 0" \
             f" and Data ge '{HOJE - dt.timedelta(days=dias):%Y-%m-%d}'"
    q = urllib.parse.urlencode({'$filter': filtro, '$orderby': 'Data desc', '$format': 'json', '$top': '2000',
                                '$select': 'Indicador,Data,Mediana,Minimo,Maximo,numeroRespondentes'}, quote_via=urllib.parse.quote)
    f = baixar('https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais?' + q,
               BRUTOS / 'bcb' / f'focus_{ano}.json', sempre=True)
    return json.loads(f.read_text('utf-8'))['value']


def ultimo(serie, d, limite=5):
    """Último valor até d, sem olhar mais de `limite` dias para trás (feriado lá fora, fundo que atrasou)."""
    for k in range(limite + 1):
        v = serie.get(d - dt.timedelta(days=k))
        if v is not None:
            return v
    return None


def main():
    cdi, ptax = sgs(12), sgs(1)
    y2, y10 = fred('DGS2'), fred('DGS10')
    cotas = cvm()
    termo, pre2, inf3, juro_real = tesouro()
    dias = sorted(d for d in cdi if d >= INICIO)
    # o último dia é o último em que todas as séries existem (os fundos chegam à CVM com 1–2 dias de atraso)
    # e cada fundo tem de ter a cota daquele dia mesmo: repetir a anterior daria variação zero falsa no fim da série
    while dias and (any(ultimo(s, dias[-1], 4) is None for s in (ptax, y2, y10))
                    or any(dias[-1] not in c for c in cotas.values()) or dias[-1] not in ptax):
        dias.pop()
    ret = {k: [] for k in ('cdi', 'lci', 'credito', 'irfm', 'imab5', 'imab5mais', 'imab', 'ibov', 'sp_reais', 'dolar_puro', 'tsy2', 'tsy10')}
    for a, b in zip(dias, dias[1:]):
        r_cdi = cdi[b] / 100  # a taxa do dia b remunera de a para b
        fx = ultimo(ptax, b) / ultimo(ptax, a) - 1
        ret['cdi'].append(r_cdi)
        ret['lci'].append(LCI_CDI * r_cdi)
        for k in ('credito', 'irfm', 'imab5', 'imab5mais', 'imab', 'ibov'):
            ret[k].append(ultimo(cotas[k], b) / ultimo(cotas[k], a) - 1)
        ret['sp_reais'].append(ultimo(cotas['global'], b) / ultimo(cotas['global'], a) - 1)
        ret['dolar_puro'].append(fx)
        for k, y, anos in (('tsy2', y2, 2), ('tsy10', y10, 10)):
            ret[k].append((1 + titulo(ultimo(y, a), ultimo(y, b), (b - a).days, anos)) * (1 + fx) - 1)
    FONTE = {'LCI/LCA': 'lci', 'Tesouro Selic': 'cdi', 'Crédito high grade': 'credito', 'Prefixado 2 anos': 'irfm',
             'IPCA+ 3 anos': 'imab5', 'IPCA+ 10 anos': 'imab5mais', 'Bolsa Brasil': 'ibov',
             'Bolsa global sem hedge': 'sp_reais', 'Treasury 2 anos': 'tsy2', 'Treasury 10 anos': 'tsy10'}

    PESOS_DIA = {}

    def carteira(col):
        """Valor diário com os pesos `col` (0 carteira, 1 régua), rebalanceado no 1º dia útil de cada mês."""
        w = {c: PESOS[c][col] / 100 for c in PESOS}
        valor, nivel = [100.0], {c: w[c] * 100 for c in PESOS}
        pesos = []
        for i, (a, b) in enumerate(zip(dias, dias[1:])):
            if b.month != a.month:
                total = sum(nivel.values())
                nivel = {c: w[c] * total for c in PESOS}
            tot = sum(nivel.values())
            pesos.append({c: v / tot for c, v in nivel.items()})   # peso efetivo no início do dia b
            nivel = {c: v * (1 + ret[FONTE[c]][i]) for c, v in nivel.items()}
            valor.append(sum(nivel.values()))
        tot = sum(nivel.values())
        PESOS_DIA[col] = pesos + [{c: v / tot for c, v in nivel.items()}]   # o último: os pesos de hoje, depois da deriva
        return valor

    def indice(k):
        v = [100.0]
        for r in ret[k]:
            v.append(v[-1] * (1 + r))
        return v

    s = {'carteira': carteira(0), 'regua': carteira(1), 'cdi': indice('cdi'), 'ibovespa': indice('ibov'),
         'imab': indice('imab'), 'dolar': indice('dolar_puro'), 'sp500_reais': indice('sp_reais')}
    linhas = [dict(data=d.isoformat(), fase='ativa' if d >= ATIVA else 'simulada',
                   **{k: round(v[i], 4) for k, v in s.items()}) for i, d in enumerate(dias)]

    # classes: retorno do último dia, em 12 meses e desde a data de corte
    def acum(k, desde):
        i0 = max(i for i, d in enumerate(dias) if d <= desde) if desde >= dias[0] else 0
        return (math.prod(1 + r for r in ret[k][i0:]) - 1) * 100
    def acum_entre(k, d0, d1):
        i0 = max(i for i, d in enumerate(dias) if d <= d0)
        i1 = max(i for i, d in enumerate(dias) if d <= d1)
        return (math.prod(1 + r for r in ret[k][i0:i1]) - 1) * 100
    # 21/11/2026: nenhum número atravessa a data de corte (GIPS 1.A.27; ANBIMA, Regras de Publicidade, art. 37 §2º):
    # os 12 meses são os da simulação, até 13/11/2026; o real é medido só desde 13/11/2026
    classes = [dict(classe=c, carteira=PESOS[c][0], regua=PESOS[c][1], desvio=PESOS[c][0] - PESOS[c][1],
                    ret_dia=round(ret[FONTE[c]][-1] * 100, 3) if dias[-1] > ATIVA else None,
                    ret_12m_sim=round(acum_entre(FONTE[c], ATIVA - dt.timedelta(days=365), ATIVA), 2),
                    ret_ativa=round(acum(FONTE[c], ATIVA), 3) if dias[-1] > ATIVA else None, como=PESOS[c][2])
               for c in PESOS]

    # ---------------------------------------------------------------- fase 2 (21/11/2026)
    i_corte = max(i for i, d in enumerate(dias) if d <= ATIVA)
    i_12 = max(i for i, d in enumerate(dias) if d <= ATIVA - dt.timedelta(days=365))
    ativa = dias[-1] > ATIVA
    rets = {k: [b / a - 1 for a, b in zip(v, v[1:])] for k, v in s.items()}   # rets[k][i]: do dia i ao dia i+1

    # mês a mês: carteira, régua, CDI e IPCA; o mês de 13/11/2026 é partido em simulação e real
    ipca = {(d.year, d.month): v for d, v in sgs(433).items()}
    mensal = []
    marcos = [i for i in range(1, len(dias)) if dias[i].month != dias[i - 1].month]
    fim = len(dias) - 1
    blocos = list(zip([0] + [m - 1 for m in marcos], [m - 1 for m in marcos] + [fim]))   # (fechamento anterior, último dia do mês)
    for j, (b0, b1) in enumerate(blocos):
        partes = [(b0, i_corte, 'simulada'), (i_corte, b1, 'ativa')] if b0 < i_corte < b1 else [(b0, b1, 'ativa' if b0 >= i_corte else 'simulada')]
        for p0, p1, fase in partes:
            if p1 <= p0:
                continue
            m = (dias[p1].year, dias[p1].month)
            parcial = len(partes) > 1 or j == 0 or (j == len(blocos) - 1)
            mensal.append(dict(mes=f'{m[0]}-{m[1]:02d}', de=dias[p0].isoformat(), ate=dias[p1].isoformat(), fase=fase, parcial=parcial,
                               **{k: round((s[k][p1] / s[k][p0] - 1) * 100, 3) for k in ('carteira', 'regua', 'cdi')},
                               ipca=None if parcial else ipca.get(m)))

    # risco: só na simulação (até 13/11/2026)
    def risco(k):
        r = rets[k][:i_corte]
        c = rets['cdi'][:i_corte]
        ex = [a - b for a, b in zip(r, c)]
        sr_d = st.mean(ex) / st.stdev(ex) if st.stdev(ex) > 0 else None
        neg = math.sqrt(sum(min(0.0, e) ** 2 for e in ex) / len(ex))
        pico, queda = -1e9, 0.0
        for v in s[k][:i_corte + 1]:
            pico = max(pico, v)
            queda = min(queda, v / pico - 1)
        meses_ok = [x for x in mensal if x['fase'] == 'simulada' and not x['parcial']]
        r_sorted = sorted(r)
        out = dict(vol=st.stdev(r) * math.sqrt(252) * 100,
                   sharpe=sr_d * math.sqrt(252) if sr_d is not None else None,
                   sharpe_ep=math.sqrt(252) * math.sqrt((1 + (sr_d or 0) ** 2 / 2) / len(ex)) if sr_d is not None else None,
                   sortino=st.mean(ex) * 252 / (neg * math.sqrt(252)) if neg > 0 else None,
                   queda=queda * 100,
                   acerto=100 * sum(1 for x in meses_ok if x[k] > x['cdi']) / len(meses_ok) if k != 'cdi' else None,
                   var95=-r_sorted[int(0.05 * len(r_sorted))] * 100 if k != 'cdi' else None)
        if k == 'carteira':
            d = [a - b for a, b in zip(r, rets['regua'][:i_corte])]
            te = st.stdev(d) * math.sqrt(252)
            out.update(te=te * 100, ir=st.mean(d) * 252 / te if te > 0 else None)
        return out
    risco_linhas = []
    rk = {k: risco(k) for k in ('carteira', 'regua', 'cdi')}
    for medida, chave, formula in (
            ('Volatilidade ao ano', 'vol', 'desvio-padrão dos retornos diários × √252'),
            ('Sharpe (contra o CDI)', 'sharpe', 'média do retorno acima do CDI ÷ desvio desse excesso, anualizado'),
            ('Margem de erro do Sharpe', 'sharpe_ep', 'erro-padrão de Lo (2002): √[(1 + SR²/2) ÷ T], anualizado'),
            ('Sortino (contra o CDI)', 'sortino', 'como o Sharpe, mas só os dias abaixo do CDI entram no risco'),
            ('Maior queda', 'queda', 'do pico ao fundo, no nível da série'),
            ('Meses acima do CDI', 'acerto', '% dos meses cheios em que a série rendeu mais que o CDI'),
            ('VaR 95% de 1 dia', 'var95', 'perda diária que só foi superada em 5% dos dias'),
            ('Tracking error contra a régua', 'te', 'desvio-padrão da diferença diária para a régua × √252'),
            ('Information ratio contra a régua', 'ir', 'diferença média para a régua ÷ tracking error, anualizado')):
        risco_linhas.append(dict(medida=medida, chave=chave, formula=formula,
                                 **{k: (round(rk[k][chave], 3) if rk[k].get(chave) is not None else None) for k in rk}))

    # atribuição contra a régua (Brinson-Fachler; a seleção é zero porque carteira e régua usam o mesmo índice por classe)
    def atribuicao(i0, i1):
        efeito = {c: 0.0 for c in PESOS}
        for i in range(i0, i1):
            wj, wr = PESOS_DIA[0][i], PESOS_DIA[1][i]
            rr = rets['regua'][i]
            for c in PESOS:
                efeito[c] += (wj[c] - wr[c]) * (ret[FONTE[c]][i] - rr)
        dif = (s['carteira'][i1] / s['carteira'][i0] - s['regua'][i1] / s['regua'][i0]) * 100
        soma = sum(efeito.values()) * 100
        return [dict(classe=c, efeito=round(efeito[c] * 100, 3)) for c in PESOS] + [dict(classe='Juros compostos e rebalanceamento', efeito=round(dif - soma, 3)),
                                                                                       dict(classe='Total: carteira menos régua', efeito=round(dif, 3))]
    atrib = [dict(periodo='sim12', **x) for x in atribuicao(i_12, i_corte)]
    if ativa:
        atrib += [dict(periodo='real', **x) for x in atribuicao(i_corte, len(dias) - 1)]

    # deriva dos pesos e efeito câmbio por classe
    fx_idx = s['dolar']
    def separa(k, i0, i1):
        brl = math.prod(1 + r for r in ret[k][i0:i1])
        cambio = fx_idx[i1] / fx_idx[i0]
        return round((brl / cambio - 1) * 100, 3), round((cambio - 1) * 100, 3)
    for linha in classes:
        c = linha['classe']
        linha['peso_hoje'] = round(PESOS_DIA[0][-1][c] * 100, 2)
        if FONTE[c] in ('sp_reais', 'tsy2', 'tsy10'):
            linha['sim12_moeda'], linha['sim12_cambio'] = separa(FONTE[c], i_12, i_corte)
            if ativa:
                linha['ativa_moeda'], linha['ativa_cambio'] = separa(FONTE[c], i_corte, len(dias) - 1)

    # líquido de IR estimado "se resgatado hoje" (regras de 2026: a MP 1.303/2025 caducou em 08/10/2025)
    def aliquota(c, dias_corridos):
        if c == 'LCI/LCA':
            return 0.0
        if c in ('Bolsa Brasil', 'Bolsa global sem hedge', 'Treasury 2 anos', 'Treasury 10 anos'):
            return 0.15
        return 0.225 if dias_corridos <= 180 else 0.20 if dias_corridos <= 360 else 0.175 if dias_corridos <= 720 else 0.15
    def liquido(i0, i1, col):
        dc = (dias[i1] - dias[i0]).days
        w = {c: PESOS[c][col] / 100 for c in PESOS}
        bruto = s['carteira' if col == 0 else 'regua'][i1] / s['carteira' if col == 0 else 'regua'][i0] - 1
        imposto = 0.0
        for c in PESOS:
            rc = math.prod(1 + r for r in ret[FONTE[c]][i0:i1]) - 1
            imposto += w[c] * max(rc, 0.0) * aliquota(c, dc)
        iof = 0.011 * sum(w[c] for c in ('Treasury 2 anos', 'Treasury 10 anos'))   # IOF de 1,1% na remessa, uma vez
        cdi_b = s['cdi'][i1] / s['cdi'][i0] - 1
        return dict(bruto=round(bruto * 100, 3), imposto=round(-imposto * 100, 3), iof=round(-iof * 100, 3),
                    liquido=round((bruto - imposto - iof) * 100, 3), cdi_bruto=round(cdi_b * 100, 3),
                    cdi_liquido=round(cdi_b * (1 - aliquota('CDI', dc)) * 100, 3), dias=dc)
    liq = [dict(periodo='sim12', serie='Carteira', **liquido(i_12, i_corte, 0)), dict(periodo='sim12', serie='Régua', **liquido(i_12, i_corte, 1))]
    if ativa:
        liq += [dict(periodo='real', serie='Carteira', **liquido(i_corte, len(dias) - 1, 0)), dict(periodo='real', serie='Régua', **liquido(i_corte, len(dias) - 1, 1))]

    # premissas do case contra o Focus (2027) e contra o que já aconteceu
    fz = focus(['Selic', 'IPCA', 'Câmbio'], '2027')
    selic_meta = sgs(432)
    selic_hoje = selic_meta[max(d for d in selic_meta if d <= HOJE)]
    ipca12 = sgs(13522)
    CASO = {'Selic': (12.0, 13.5, 15.5), 'IPCA': (4.6, 5.5, 7.1), 'Câmbio': (5.10, 5.67, 6.80)}
    hoje_mercado = {'Selic': (selic_hoje, 'Selic meta hoje'), 'IPCA': (ipca12[max(ipca12)], f'IPCA 12 meses até {max(ipca12):%m/%Y}'),
                    'Câmbio': (ultimo(ptax, max(ptax)), f'PTAX de {max(ptax):%d/%m}')}
    NOMES_C = ('Ajuste crível', 'Já apresentado', 'Deterioração')
    premissas, focus_hist = [], []
    for ind, (a, b, c3) in CASO.items():
        linhas_f = sorted((x for x in fz if x['Indicador'] == ind), key=lambda x: x['Data'])
        if not linhas_f:
            continue
        u = linhas_f[-1]
        perto = min(range(3), key=lambda k: abs(CASO[ind][k] - u['Mediana']))
        premissas.append(dict(indicador=ind, ajuste=a, base=b, deterioracao=c3, focus=u['Mediana'], focus_min=u['Minimo'], focus_max=u['Maximo'],
                              respondentes=u['numeroRespondentes'], focus_data=u['Data'], mercado=round(hoje_mercado[ind][0], 4),
                              mercado_rotulo=hoje_mercado[ind][1], cenario_focus=NOMES_C[perto]))
        sem = {}
        for x in linhas_f:   # uma leitura por semana (a sexta de referência do Focus)
            sem[dt.date.fromisoformat(x['Data']).isocalendar()[:2]] = x
        focus_hist += [dict(indicador=ind, data=x['Data'], mediana=x['Mediana']) for x in sem.values()]

    # ---------------------------------------------------------------- estresse (21/11/2026)
    def idx(d):
        return max(i for i, x in enumerate(dias) if x <= d)

    def episodio(nome, i0, i1):
        contrib = {c: PESOS[c][0] / 100 * (math.prod(1 + r for r in ret[FONTE[c]][i0:i1]) - 1) * 100 for c in PESOS}
        p0, p1 = ultimo(pre2, dias[i0]), ultimo(pre2, dias[i1])
        pior, melhor = min(contrib, key=contrib.get), max(contrib, key=contrib.get)
        return dict(nome=nome, de=dias[i0].isoformat(), ate=dias[i1].isoformat(), dias_uteis=i1 - i0,
                    **{k: round((s[k][i1] / s[k][i0] - 1) * 100, 3) for k in ('carteira', 'regua', 'cdi', 'ibovespa', 'dolar')},
                    pre_2a=round(p1 - p0, 2) if p0 and p1 else None,
                    pior=pior, pior_pp=round(contrib[pior], 3), melhor=melhor, melhor_pp=round(contrib[melhor], 3))
    NOMEADOS = [('Pacote fiscal e alta do dólar', dt.date(2024, 11, 26), dt.date(2024, 12, 18)),
                ('Eleição: do 1º ao 2º turno', dt.date(2026, 10, 2), dt.date(2026, 10, 26))]
    janelas = [(idx(a), idx(b), nome) for nome, a, b in NOMEADOS if dias[0] <= a and b <= ATIVA]
    N = 10   # as piores janelas de 10 dias úteis da carteira, sem sobrepor as outras
    candidatas = sorted(range(0, i_corte - N + 1), key=lambda i: s['carteira'][i + N] / s['carteira'][i])
    piores = []
    for i in candidatas:
        if len(piores) == 3:
            break
        if all(i + N <= a or i >= b for a, b, _ in janelas + piores):
            piores.append((i, i + N, None))
    episodios = [episodio(nome, a, b) for a, b, nome in janelas]
    episodios += [episodio(f'Pior quinzena nº {k + 1} da carteira', a, b) for k, (a, b, _) in enumerate(sorted(piores, key=lambda p: s['carteira'][p[1]] / s['carteira'][p[0]]))]

    # choques hipotéticos, lineares pela duration de hoje (e beta 1 nas bolsas)
    def dmod_par(y, prazo):
        y = y / 100
        return (1 - (1 + y / 2) ** (-2 * prazo)) / y
    y_pre = pre2[max(pre2)]
    r3, r10 = juro_real[max(juro_real)]
    D = {'Prefixado 2 anos': 2 / (1 + y_pre / 100), 'IPCA+ 3 anos': 3 / (1 + r3 / 100), 'IPCA+ 10 anos': 10 / (1 + r10 / 100),
         'Treasury 2 anos': dmod_par(y2[max(y2)], 2), 'Treasury 10 anos': dmod_par(y10[max(y10)], 10)}
    USD = ('Bolsa global sem hedge', 'Treasury 2 anos', 'Treasury 10 anos')

    def choque(local, cambio=0.0):
        imp = {c: ((1 + local.get(c, 0.0) / 100) * (1 + (cambio if c in USD else 0.0) / 100) - 1) * 100 for c in PESOS}
        cart = {c: PESOS[c][0] / 100 * imp[c] for c in PESOS}
        maior = max(cart, key=lambda c: abs(cart[c]))
        return dict(carteira=round(sum(cart.values()), 3), regua=round(sum(PESOS[c][1] / 100 * imp[c] for c in PESOS), 3),
                    maior=maior, maior_pp=round(cart[maior], 3))
    pre_up = {'Prefixado 2 anos': -D['Prefixado 2 anos'] * 2}
    real_up = {'IPCA+ 3 anos': -D['IPCA+ 3 anos'] * 1, 'IPCA+ 10 anos': -D['IPCA+ 10 anos'] * 1}
    tsy = lambda dp: {'Treasury 2 anos': -D['Treasury 2 anos'] * dp, 'Treasury 10 anos': -D['Treasury 10 anos'] * dp}  # noqa: E731
    f2 = lambda v: f'{v:.2f}'.replace('.', ',')  # noqa: E731
    choques = [
        dict(nome='Juro prefixado +2 p.p.', tipo='simples', como=f'LTN de 2 anos, duration {f2(D["Prefixado 2 anos"])}', **choque(pre_up)),
        dict(nome='Juro real (IPCA+) +1 p.p.', tipo='simples', como=f'NTN-B de ~3 e ~10 anos, durations {f2(D["IPCA+ 3 anos"])} e {f2(D["IPCA+ 10 anos"])}', **choque(real_up)),
        dict(nome='Dólar +10%', tipo='simples', como='bolsa global e Treasuries, sem hedge', **choque({}, 10)),
        dict(nome='Ibovespa −15%', tipo='simples', como='beta 1', **choque({'Bolsa Brasil': -15})),
        dict(nome='Bolsa americana −15% em dólar', tipo='simples', como='beta 1, em dólar', **choque({'Bolsa global sem hedge': -15})),
        dict(nome='Juro dos Treasuries +1 p.p.', tipo='simples', como=f'durations {f2(D["Treasury 2 anos"])} e {f2(D["Treasury 10 anos"])}', **choque(tsy(1))),
        dict(nome='Combinado: estresse doméstico', tipo='combinado', como='juro prefixado +2 p.p., juro real +1 p.p., dólar +10%, Ibovespa −15%',
             **choque({**pre_up, **real_up, 'Bolsa Brasil': -15}, 10)),
        dict(nome='Combinado: aversão global', tipo='combinado', como='bolsa americana −15%, juro dos Treasuries −0,5 p.p., dólar +10%, Ibovespa −15%',
             **choque({'Bolsa global sem hedge': -15, 'Bolsa Brasil': -15, **tsy(-0.5)}, 10)),
    ]

    # monitor: juro do prefixado de 2 anos (Tesouro Direto) e dólar
    t_dias = sorted(pre2)
    monitor = [dict(indicador='Prefixado de 2 anos', unidade='% a.a.', valor=round(pre2[t_dias[-1]], 2),
                    data=t_dias[-1].isoformat(), realizar=REALIZAR, sair=SAIR,
                    fonte='LTNs do Tesouro Direto, interpoladas a 2 anos (meio entre compra e venda)'),
               dict(indicador='Dólar (PTAX)', unidade='R$', valor=round(ultimo(ptax, max(ptax)), 4),
                    data=max(ptax).isoformat(), realizar=None, sair=None, fonte='BCB, série 1')]
    termo_linhas = [dict(data=d.isoformat(), pre_2a=round(pre2[d], 3), fwd_1a1a=round(termo[d], 3) if d in termo else None,
                         infl_impl_3a=round(inf3[d], 3) if d in inf3 else None) for d in t_dias if d >= HOJE - dt.timedelta(days=200)]

    # frescor: a data do último dado de cada fonte e quantos dias úteis ela está atrás de hoje
    def atraso(d):
        n, x = 0, d
        while x < HOJE:
            x += dt.timedelta(days=1)
            if x.weekday() < 5 and x not in FERIADOS:
                n += 1
        return n
    fontes = []
    for fonte, dado, ult, limite in (('Banco Central', 'CDI (SGS 12)', max(cdi), 2),
                                     ('Banco Central', 'dólar PTAX (SGS 1)', max(ptax), 2),
                                     ('CVM', 'cotas dos 7 fundos usados como índice', min(max(c) for c in cotas.values()), 4),
                                     ('Tesouro Direto', 'taxas das LTNs', max(pre2), 3),
                                     ('FRED', 'juro dos Treasuries (DGS2, DGS10)', min(max(y2), max(y10)), 3),
                                     ('Banco Central', 'Focus (expectativas de 2027)', max(dt.date.fromisoformat(x['Data']) for x in fz), 6)):
        fontes.append(dict(fonte=fonte, dado=dado, ultimo=ult.isoformat(), atraso=atraso(ult), limite=limite,
                           status='ok' if atraso(ult) <= limite else 'atrasado'))
    fontes.append(dict(fonte='Painel', dado='página gerada', ultimo=HOJE.isoformat(), atraso=0, limite=3,
                       status='ok', gerado=dt.datetime.now(BRT).strftime('%d/%m/%Y %H:%M'), serie_ate=dias[-1].isoformat()))

    codigo = Path(__file__).read_text('utf-8')
    comum = [dict(label='atualizar.py', value=codigo, type='code', lang='python'),
             dict(label='Atualizado em', value=dt.datetime.now(BRT).strftime('%d/%m/%Y %H:%M')),
             dict(label='Último dia com todas as séries', value=dias[-1].strftime('%d/%m/%Y'))]
    DADOS.mkdir(parents=True, exist_ok=True)
    for nome, rows in (('serie', linhas), ('classes', classes), ('monitor', monitor), ('termo', termo_linhas), ('fontes', fontes),
                       ('mensal', mensal), ('risco', risco_linhas), ('atribuicao', atrib), ('liquido', liq),
                       ('premissas', premissas), ('focus', focus_hist), ('episodios', episodios), ('choques', choques), ('tese', [TESE])):
        (DADOS / f'{nome}.json').write_text(json.dumps({'rows': rows, 'meta': comum}, ensure_ascii=False), 'utf-8')
    fim = linhas[-1]
    print(f"ok: {len(linhas)} dias, de {linhas[0]['data']} a {fim['data']} | carteira {fim['carteira']:.2f} régua {fim['regua']:.2f} "
          f"CDI {fim['cdi']:.2f} Ibov {fim['ibovespa']:.2f} IMA-B {fim['imab']:.2f} dólar {fim['dolar']:.2f} global {fim['sp500_reais']:.2f} | "
          f"fontes atrasadas: {[x['dado'] for x in fontes if x['status'] != 'ok'] or 'nenhuma'} | "
          f"prefixado 2a {monitor[0]['valor']} em {monitor[0]['data']}")


if __name__ == '__main__':
    main()
