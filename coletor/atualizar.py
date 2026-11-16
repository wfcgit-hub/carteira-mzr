"""Painel da carteira MZR: coleta os dados públicos, calcula carteira, régua e índices e grava os arquivos do painel.

Uso: python -I -X utf8 atualizar.py <pasta do painel>
     (-I: os arquivos baixados são dados de terceiros; o script não importa nada da pasta deles)

Fontes, todas públicas e automáticas (nada da B3, da Anbima, do MSCI, da CME, da ICE ou do Yahoo):
- Banco Central, SGS: CDI diário (série 12) e dólar PTAX de venda (série 1);
- FRED (Fed de St. Louis): S&P 500 (SP500, índice de preço, sem dividendos), Fed Funds efetivo (DFF) e juro dos
  Treasuries de 2 e de 10 anos (DGS2, DGS10);
- CVM, dados abertos: cotas diárias de fundos passivos, que fazem as vezes dos índices da B3 e da Anbima
  (cotas líquidas de taxa de administração; ver FUNDOS);
- Tesouro Transparente: preços e taxas do Tesouro Direto, para o juro de 1 ano a termo (LTNs de ~1 e ~2 anos).

A carteira e a régua são as do case (slides 9 e 10), antes do IR, rebalanceadas no 1º dia útil de cada mês.
Até 13/11/2026, a data de corte do case, a carteira não existia: o trecho é uma simulação com os pesos de hoje.
Dali em diante, o painel acompanha a carteira de fato.
"""
import csv
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
HOJE = dt.date.today()

FUNDOS = {  # classe: (CNPJ, nome do fundo) — fundos de investimento que reportam cota diária à CVM (os ETFs não reportam)
    'ibov': ('24546223000117', 'Itaú Vértice Ibovespa Equities FIF Ações'),
    'imab': ('10740658000193', 'Caixa Brasil IMA-B Títulos Públicos FIF RF'),
    'imab5': ('11060913000110', 'Caixa Brasil IMA-B 5 Títulos Públicos FIF RF'),
    'imab5mais': ('13327340000173', 'BB Previdenciário RF IMA-B 5+ Títulos Públicos FIF'),
    'irfm': ('14508605000100', 'Caixa Brasil IRF-M Títulos Públicos FIF RF'),
    'credito': ('07096435000120', 'Itaú High Grade RF Crédito Privado FIF'),
}
LCI_CDI = 0.866   # LCI/LCA de banco grande: 86,6% do CDI (Quantum, via Seu Dinheiro, 24/09/2026)
# classe: (peso na carteira, peso na régua, como a série é feita)
PESOS = {  # carteira J do case (16/11/2026)
    'LCI/LCA': (30.0, 30.0, '86,6% do CDI (BCB, série 12)'),
    'Tesouro Selic': (12.5, 20.0, 'CDI (BCB, série 12)'),
    'Crédito high grade': (12.5, 10.0, 'cota do ' + FUNDOS['credito'][1]),
    'Prefixado': (20.0, 7.5, 'cota do ' + FUNDOS['irfm'][1]),
    'IPCA+ curto': (7.5, 7.5, 'cota do ' + FUNDOS['imab5'][1]),
    'IPCA+ longo': (0.0, 7.5, 'cota do ' + FUNDOS['imab5mais'][1]),
    'Ações Brasil': (2.5, 5.0, 'cota do ' + FUNDOS['ibov'][1]),
    'Bolsa global sem hedge': (2.5, 7.5, 'S&P 500 convertido pela PTAX (FRED, BCB)'),
    'Treasury 2 anos': (2.5, 5.0, 'Treasury de 2 anos pelo juro (FRED, DGS2), convertido pela PTAX (BCB)'),
    'Treasury 10 anos': (10.0, 0.0, 'Treasury de 10 anos pelo juro (FRED, DGS10), convertido pela PTAX (BCB)'),
}
assert abs(sum(p[0] for p in PESOS.values()) - 100) < 1e-9 and abs(sum(p[1] for p in PESOS.values()) - 100) < 1e-9
REALIZAR, SAIR = 13.8, 16.0   # gatilhos do slide do prefixado curto: o juro do cenário base e o da deterioração, 16/11


# ------------------------------------------------------------------ coleta
def baixar(url, destino, sempre=False, tentativas=3):
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
                raise RuntimeError(f'falhou {url}: {erro}')
            time.sleep(5 * (k + 1))


def sgs(codigo):
    ini = (INICIO - dt.timedelta(days=10)).strftime('%d/%m/%Y')
    url = f'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados?formato=json&dataInicial={ini}&dataFinal={HOJE:%d/%m/%Y}'
    f = baixar(url, BRUTOS / 'bcb' / f'sgs{codigo}.json', sempre=True)
    return {dt.datetime.strptime(x['data'], '%d/%m/%Y').date(): float(x['valor']) for x in json.loads(f.read_text('utf-8'))}


def fred(serie):
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie}&cosd={INICIO - dt.timedelta(days=10):%Y-%m-%d}'
    f = baixar(url, BRUTOS / 'fred' / f'{serie}.csv', sempre=True)
    out = {}
    for linha in list(csv.reader(io.StringIO(f.read_text('utf-8'))))[1:]:
        if len(linha) == 2 and linha[1] not in ('', '.'):
            out[dt.date.fromisoformat(linha[0])] = float(linha[1])
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
    for l in r:
        if l[0] != 'Tesouro Prefixado':
            continue
        base = dt.datetime.strptime(l[2], '%d/%m/%Y').date()
        if base < HOJE - dt.timedelta(days=400):
            continue
        venc = dt.datetime.strptime(l[1], '%d/%m/%Y').date()
        tc, tv = num(l[3]), num(l[4])
        taxa = (tc + tv) / 2 if (tc and tv) else (tc or tv)   # meio entre a compra e a venda do investidor
        if taxa:
            por_dia.setdefault(base, []).append(((venc - base).days / 365.25, taxa))
    termo, pre2 = {}, {}
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
    return termo, pre2


# ------------------------------------------------------------------ cálculo
def titulo(a, b, dias, anos):
    """Retorno de um Treasury de prazo constante entre dois dias, pelo juro (%): carrego menos duração × variação,
    com convexidade; título ao par com cupom semestral (a mesma conta do bloco 11 do case, em base diária)."""
    a, b = a / 100, b / 100
    n = 2 * anos
    dur = (1 - (1 + a / 2) ** -n) / (a / 2) / 2 / (1 + a / 2)
    return a * dias / 365 - dur * (b - a) + 0.5 * dur ** 2 * (b - a) ** 2


def ultimo(serie, d, limite=5):
    """Último valor até d, sem olhar mais de `limite` dias para trás (feriado lá fora, fundo que atrasou)."""
    for k in range(limite + 1):
        v = serie.get(d - dt.timedelta(days=k))
        if v is not None:
            return v
    return None


def main():
    cdi, ptax = sgs(12), sgs(1)
    sp, dff, y2, y10 = fred('SP500'), fred('DFF'), fred('DGS2'), fred('DGS10')
    cotas = cvm()
    termo, pre2 = tesouro()
    dias = sorted(d for d in cdi if d >= INICIO)
    # o último dia é o último em que todas as séries existem (os fundos chegam à CVM com 1–2 dias de atraso)
    # e cada fundo tem de ter a cota daquele dia mesmo: repetir a anterior daria variação zero falsa no fim da série
    while dias and (any(ultimo(s, dias[-1], 4) is None for s in (ptax, sp, dff, y2, y10))
                    or any(dias[-1] not in c for c in cotas.values()) or dias[-1] not in ptax):
        dias.pop()
    ret = {k: [] for k in ('cdi', 'lci', 'credito', 'irfm', 'imab5', 'imab5mais', 'imab', 'ibov', 'sp_reais', 'sp_hedge', 'dolar', 'dolar_puro', 'tsy2', 'tsy10')}
    for a, b in zip(dias, dias[1:]):
        r_cdi = cdi[b] / 100  # a taxa do dia b remunera de a para b
        fx = ultimo(ptax, b) / ultimo(ptax, a) - 1
        rsp = ultimo(sp, b) / ultimo(sp, a) - 1
        ff = ultimo(dff, a) / 100 * (b - a).days / 360
        ret['cdi'].append(r_cdi)
        ret['lci'].append(LCI_CDI * r_cdi)
        for k in ('credito', 'irfm', 'imab5', 'imab5mais', 'imab', 'ibov'):
            ret[k].append(ultimo(cotas[k], b) / ultimo(cotas[k], a) - 1)
        ret['sp_reais'].append((1 + rsp) * (1 + fx) - 1)
        ret['sp_hedge'].append((1 + rsp) * (1 + r_cdi - ff) - 1)
        ret['dolar'].append((1 + fx) * (1 + ff) - 1)
        ret['dolar_puro'].append(fx)
        for k, y, anos in (('tsy2', y2, 2), ('tsy10', y10, 10)):
            ret[k].append((1 + titulo(ultimo(y, a), ultimo(y, b), (b - a).days, anos)) * (1 + fx) - 1)
    FONTE = {'LCI/LCA': 'lci', 'Tesouro Selic': 'cdi', 'Crédito high grade': 'credito', 'Prefixado': 'irfm',
             'IPCA+ curto': 'imab5', 'IPCA+ longo': 'imab5mais', 'Ações Brasil': 'ibov',
             'Bolsa global sem hedge': 'sp_reais', 'Treasury 2 anos': 'tsy2', 'Treasury 10 anos': 'tsy10'}

    def carteira(col):
        """Valor diário com os pesos `col` (0 carteira, 1 régua), rebalanceado no 1º dia útil de cada mês."""
        w = {c: PESOS[c][col] / 100 for c in PESOS}
        valor, nivel = [100.0], {c: w[c] * 100 for c in PESOS}
        for i, (a, b) in enumerate(zip(dias, dias[1:])):
            if b.month != a.month:
                total = sum(nivel.values())
                nivel = {c: w[c] * total for c in PESOS}
            nivel = {c: v * (1 + ret[FONTE[c]][i]) for c, v in nivel.items()}
            valor.append(sum(nivel.values()))
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
    um_ano = dias[-1] - dt.timedelta(days=365)
    classes = [dict(classe=c, carteira=PESOS[c][0], regua=PESOS[c][1], desvio=PESOS[c][0] - PESOS[c][1],
                    ret_dia=round(ret[FONTE[c]][-1] * 100, 3), ret_12m=round(acum(FONTE[c], um_ano), 2),
                    ret_ativa=round(acum(FONTE[c], ATIVA), 3) if dias[-1] > ATIVA else None, como=PESOS[c][2])
               for c in PESOS]

    # monitor: juro do prefixado de 2 anos (Tesouro Direto) e dólar
    t_dias = sorted(pre2)
    monitor = [dict(indicador='Prefixado de 2 anos', unidade='% a.a.', valor=round(pre2[t_dias[-1]], 2),
                    data=t_dias[-1].isoformat(), realizar=REALIZAR, sair=SAIR,
                    fonte='LTNs do Tesouro Direto, interpoladas a 2 anos (meio entre compra e venda)'),
               dict(indicador='Dólar (PTAX)', unidade='R$', valor=round(ultimo(ptax, max(ptax)), 4),
                    data=max(ptax).isoformat(), realizar=None, sair=None, fonte='BCB, série 1')]
    termo_linhas = [dict(data=d.isoformat(), pre_2a=round(pre2[d], 3)) for d in t_dias if d >= HOJE - dt.timedelta(days=200)]

    codigo = Path(__file__).read_text('utf-8')
    comum = [dict(label='atualizar.py', value=codigo, type='code', lang='python'),
             dict(label='Atualizado em', value=dt.datetime.now().strftime('%d/%m/%Y %H:%M')),
             dict(label='Último dia com todas as séries', value=dias[-1].strftime('%d/%m/%Y'))]
    DADOS.mkdir(parents=True, exist_ok=True)
    for nome, rows in (('serie', linhas), ('classes', classes), ('monitor', monitor), ('termo', termo_linhas)):
        (DADOS / f'{nome}.json').write_text(json.dumps({'rows': rows, 'meta': comum}, ensure_ascii=False), 'utf-8')
    fim = linhas[-1]
    print(f"ok: {len(linhas)} dias, de {linhas[0]['data']} a {fim['data']} | carteira {fim['carteira']:.2f} régua {fim['regua']:.2f} "
          f"CDI {fim['cdi']:.2f} Ibov {fim['ibovespa']:.2f} IMA-B {fim['imab']:.2f} dólar {fim['dolar']:.2f} S&P {fim['sp500_reais']:.2f} | "
          f"prefixado 2a {monitor[0]['valor']} em {monitor[0]['data']}")


if __name__ == '__main__':
    main()
