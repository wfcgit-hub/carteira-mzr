"""Ponte do dia: fecha a carteira nos dias em que a CVM ainda não publicou as cotas dos fundos.

Uso: python -I -X utf8 coletor/ponte_manual.py <pasta do painel> <arquivo> [<arquivo> ...]

Os arquivos são baixados à mão (a B3 e a Anbima não permitem coleta por robô):
- B3, Ibovespa, "evolução diária" do ano (CSV);
- Anbima, quadro-resumo do IMA de cada dia que falta (IRF-M, IMA-B 5, IMA-B 5+, IMA-B, variação diária);
- Anbima, quadro-resumo do IDA de cada dia que falta (IDA-DI, variação diária).
A bolsa americana sai do S&P 500 do FRED convertido pela PTAX do Banco Central (fontes abertas, baixadas aqui).

Para cada dia útil depois da última cota da CVM (dados/fontes.json), em sequência, grava em manuais/ponte.json o
retorno de cada índice desde o dia útil anterior. A ponte só preenche dia que falta: quando a CVM publica, a cota real
vence (atualizar.py).
"""
import csv
import datetime as dt
import html
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

PASTA = Path(sys.argv[1]).resolve()
ARQS = [Path(a) for a in sys.argv[2:]]
num = lambda s: float(s.replace('.', '').replace(',', '.'))  # noqa: E731


def celulas(texto):
    linhas = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', texto, re.S | re.I):
        c = [html.unescape(re.sub(r'<[^>]+>', '', x)).strip() for x in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.S | re.I)]
        c = [x for x in c if x]
        if c:
            linhas.append(c)
    return linhas


ibov, anbima = {}, {}   # ibov[data] = fechamento; anbima[(índice, data)] = variação diária em %
for a in ARQS:
    t = a.read_bytes().decode('latin-1')
    if t.startswith('IBOVESPA'):
        ano = int(re.search(r'(\d{4})', t.splitlines()[0]).group(1))
        for l in t.splitlines()[2:]:
            p = l.split(';')
            if not p[0].strip().isdigit():
                continue
            for m, v in enumerate(p[1:13], start=1):
                if v.strip():
                    ibov[dt.date(ano, m, int(p[0]))] = num(v)
        continue
    for c in celulas(t):
        # IMA: "IRF-M | 23/11/2026 | número | variação diária | ..."; IDA: "IDA - DI | 23/11/2026 | número | variação | ..."
        if len(c) >= 4 and re.fullmatch(r'\d\d/\d\d/\d{4}', c[1]):
            nome = c[0].replace(' ', '').upper()
            try:
                anbima[(nome, dt.datetime.strptime(c[1], '%d/%m/%Y').date())] = num(c[3])
            except ValueError:
                pass

fontes = json.loads((PASTA / 'dados' / 'fontes.json').read_text('utf-8'))['rows']
ult_cvm = dt.date.fromisoformat(next(r['ultimo'] for r in fontes if r['fonte'] == 'CVM'))


def baixa(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8')


ini = (ult_cvm - dt.timedelta(days=10))
sp = {dt.date.fromisoformat(l.split(',')[0]): float(l.split(',')[1]) for l in baixa(
    f'https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500&cosd={ini.isoformat()}').splitlines()[1:] if l.split(',')[1] not in ('', '.')}
ptax = {dt.datetime.strptime(x['data'], '%d/%m/%Y').date(): float(x['valor']) for x in json.loads(baixa(
    f'https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados?formato=json&dataInicial={ini:%d/%m/%Y}&dataFinal={dt.date.today():%d/%m/%Y}'))}
ate = lambda s, d: s[max(x for x in s if x <= d)]  # noqa: E731

arq = PASTA / 'manuais' / 'ponte.json'
ponte = json.loads(arq.read_text('utf-8')) if arq.exists() else {'nota': '', 'dias': {}}
dias = sorted(d for d in ibov if d > ult_cvm)
ant, feitos, faltou = ult_cvm, [], []
for d in dias:
    precisa = {'irfm': 'IRF-M', 'imab5': 'IMA-B5', 'imab5mais': 'IMA-B5+', 'imab': 'IMA-B', 'credito': 'IDA-DI'}
    falta = [n for n in precisa.values() if (n, d) not in anbima]
    if ant not in ibov or falta or d not in ptax or ant not in ptax:
        faltou.append((d, falta or ['Ibovespa ou PTAX']))
        break   # a ponte é uma corrente: sem um dia, os seguintes não fecham
    r = {k: round(anbima[(n, d)], 4) for k, n in precisa.items()}
    r['ibov'] = round((ibov[d] / ibov[ant] - 1) * 100, 4)
    r['global'] = round(((ate(sp, d) / ate(sp, ant)) * (ptax[d] / ptax[ant]) - 1) * 100, 4)
    ponte['dias'][d.isoformat()] = {'desde': ant.isoformat(), 'retornos': r,
                                    'fontes': f'Anbima (quadros-resumo do IMA e do IDA de {d:%d/%m}); B3 (Ibovespa {ibov[ant]:,.2f} → {ibov[d]:,.2f}); '
                                              f'FRED S&P 500 até {max(x for x in sp if x <= d):%d/%m} e PTAX {ptax[ant]} → {ptax[d]}'}
    feitos.append(d)
    ant = d

ponte['nota'] = ('Ponte provisória: em cada dia, o retorno desde o dia útil anterior do índice que cada fundo replica, para os dias '
                 'em que a CVM ainda não publicou a cota. Anbima e B3 baixados à mão; S&P 500 do FRED e PTAX do Banco Central. '
                 'A cota real da CVM substitui a ponte quando sai.')
arq.parent.mkdir(exist_ok=True)
arq.write_text(json.dumps(ponte, ensure_ascii=False, indent=1) + '\n', 'utf-8')
print('última cota da CVM:', ult_cvm.isoformat())
for d in feitos:
    print(d.isoformat(), ponte['dias'][d.isoformat()]['retornos'])
for d, f in faltou:
    print('faltou para', d.isoformat(), ':', ', '.join(f))
