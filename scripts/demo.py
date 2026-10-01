"""Popula um banco com dados FICTÍCIOS para demonstração do painel.

Uso: DATABASE_URL=sqlite:///demo.db python scripts/demo.py

As empresas, pessoas e valores são inventados. As regras de score e de
oportunidade aplicadas são as reais da plataforma.
"""

import random
from datetime import date, datetime, timedelta

from crosssell.config import get_settings
from crosssell.connectors.email_m365 import registrar_mensagens
from crosssell.db import SessionLocal, init_db
from crosssell.models import Empresa, Negocio, Pessoa
from crosssell.normalize import classificar_senioridade, normalizar_nome_empresa, normalizar_nome_pessoa
from crosssell.pipeline import carregar_usuarios, recalcular

random.seed(7)
HOJE = date.today()
AGORA = datetime.now()

EQUIPE = {
    "linhas_financeiras": ["victor.boldrini@innoaseguros.com.br", "pedro.acciari@innoaseguros.com.br",
                           "pamela.silva@innoaseguros.com.br"],
    "saude": ["pedro.acciari@innoaseguros.com.br", "pamela.silva@innoaseguros.com.br"],
    "ramos_elementares": ["bruno.rodrigues@innoaseguros.com.br"],
}
PRODUTOS = {
    "linhas_financeiras": ["D&O", "Cyber", "Seguro Garantia", "RC Profissional"],
    "ramos_elementares": ["Empresarial", "Transportes", "Equipamentos", "Frota"],
    "saude": ["Saúde coletivo", "Odonto coletivo", "Vida em grupo"],
}
OPERADORAS = ["Operadora A", "Operadora B", "Operadora C"]

# (nome, setor/CNAE, porte, funcionários, capital, verticais que já é cliente, verticais em funil)
EMPRESAS = [
    ("Metalúrgica Aurora", "2511000 - Fabricação de estruturas metálicas", "DEMAIS", 420, 18e6, ["saude"], []),
    ("Transportadora Rota Sul", "4930202 - Transporte rodoviário de carga", "DEMAIS", 260, 6e6, ["ramos_elementares"], []),
    ("Clínica Horizonte", "8610101 - Atividades de atendimento hospitalar", "DEMAIS", 180, 3e6, ["linhas_financeiras"], ["saude"]),
    ("Grupo Vértice Tecnologia", "6201501 - Desenvolvimento de software", "DEMAIS", 350, 12e6, ["linhas_financeiras", "saude"], []),
    ("Alimentos Serra Azul", "1091101 - Fabricação de produtos de panificação", "DEMAIS", 610, 25e6, ["ramos_elementares", "saude"], []),
    ("Construtora Pedra Alta", "4120400 - Construção de edifícios", "DEMAIS", 140, 9e6, ["linhas_financeiras"], []),
    ("Laboratório Prisma", "2121101 - Fabricação de medicamentos", "DEMAIS", 230, 15e6, ["saude"], ["linhas_financeiras"]),
    ("Varejo Bom Preço", "4711302 - Supermercados", "DEMAIS", 900, 30e6, ["ramos_elementares"], []),
    ("Agência Lumen", "7311400 - Agências de publicidade", "EPP", 35, 4e5, ["saude"], []),
    ("Energia Ventos do Norte", "3511501 - Geração de energia elétrica", "DEMAIS", 75, 40e6, ["linhas_financeiras", "ramos_elementares"], []),
    ("Escola Novo Saber", "8513900 - Ensino fundamental", "DEMAIS", 120, 2e6, ["saude"], []),
    ("Logística Ponto Certo", "5211701 - Armazéns gerais", "DEMAIS", 310, 7e6, ["saude", "ramos_elementares"], []),
    ("Fintech Ágil Pagamentos", "6619399 - Serviços financeiros", "DEMAIS", 95, 20e6, ["linhas_financeiras"], []),
    ("Hotel Mar Aberto", "5510801 - Hotéis", "DEMAIS", 160, 5e6, ["ramos_elementares"], ["saude"]),
    ("Cerâmica Terra Viva", "2342702 - Fabricação de cerâmica", "EPP", 48, 9e5, ["ramos_elementares"], []),
    ("Consultoria Ápice", "7020400 - Consultoria em gestão", "ME", 12, 1e5, ["linhas_financeiras"], []),
    # Só em prospecção: negócio aberto, nenhum ganho
    ("Indústria Química Delta", "2029100 - Fabricação de produtos químicos", "DEMAIS", 280, 22e6, [], ["ramos_elementares"]),
    ("Rede Farma Vida", "4771701 - Comércio varejista de medicamentos", "DEMAIS", 500, 14e6, [], ["saude"]),
]
NOMES = ["Ana Ribeiro", "Carlos Menezes", "Juliana Prado", "Marcos Teixeira", "Fernanda Lopes", "Rafael Duarte",
         "Patrícia Nogueira", "Eduardo Campos", "Luciana Barros", "Gustavo Pires", "Renata Moraes", "Thiago Rocha",
         "Beatriz Carvalho", "André Fontes", "Camila Freitas", "Rodrigo Sales", "Mariana Costa", "Felipe Azevedo"]
CARGOS = ["CFO", "Diretora Financeira", "Gerente de RH", "Sócio-Administrador", "Diretor de Operações",
          "Coordenadora de Benefícios", "CEO", "Analista Financeiro", "Gerente Administrativo"]


def cnpj_ficticio(i: int) -> str:
    base = [int(c) for c in f"{90000000 + i * 7919:08d}"[:8]] + [0, 0, 0, 1]
    for pesos in ([5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]):
        r = sum(d * p for d, p in zip(base, pesos)) % 11
        base.append(0 if r < 2 else 11 - r)
    return "".join(map(str, base))


def main():
    init_db()
    db = SessionLocal()
    s = get_settings()
    carregar_usuarios(db, s)
    seq = 0
    for i, (nome, cnae, porte, func, capital, clientes, funil) in enumerate(EMPRESAS):
        dominio = normalizar_nome_empresa(nome).replace(" ", "") + ".exemplo.com.br"
        e = Empresa(razao_social=f"{nome} (exemplo)", nome_normalizado=normalizar_nome_empresa(nome), cnpj=cnpj_ficticio(i),
                    dominio=dominio, cnae=cnae, porte=porte, funcionarios=func, capital_social=capital,
                    cidade="São Paulo", uf="SP", enriquecido_em=AGORA)
        db.add(e)
        db.flush()
        pessoas = []
        for j in range(random.randint(2, 4)):
            pn = NOMES[(i * 3 + j) % len(NOMES)]
            cargo = CARGOS[(i + j * 2) % len(CARGOS)]
            p = Pessoa(nome=pn, nome_normalizado=normalizar_nome_pessoa(pn), empresa_id=e.id, cargo=cargo,
                       senioridade=classificar_senioridade(cargo), fonte=random.choice(["pipedrive", "email", "receita"]),
                       email=f"{normalizar_nome_pessoa(pn).split()[0]}.{i}@{dominio}")
            db.add(p)
            pessoas.append(p)
        db.flush()

        for v in clientes:
            seq += 1
            fim = HOJE + timedelta(days=random.choice([45, 70, 95, 150, 210, 300]))
            fonte = "zeca" if v == "saude" and random.random() < 0.6 else "pipedrive"
            db.add(Negocio(empresa_id=e.id, vertical=v, fonte=fonte, id_externo=f"demo-{seq}", status="ativo" if fonte == "zeca" else "ganho",
                           titulo=random.choice(PRODUTOS[v]), produto=random.choice(PRODUTOS[v]),
                           seguradora=random.choice(OPERADORAS), inicio_vigencia=fim - timedelta(days=365),
                           fim_vigencia=fim, valor=round(random.uniform(8e3, 4e5), 2),
                           vidas=func if v == "saude" else None, responsavel_email=EQUIPE[v][0]))
        for v in funil:
            seq += 1
            db.add(Negocio(empresa_id=e.id, vertical=v, fonte="pipedrive", id_externo=f"demo-{seq}", status="aberto",
                           titulo=f"{random.choice(PRODUTOS[v])} {HOJE.year}", responsavel_email=EQUIPE[v][0]))
        if i == 5:  # ex-cliente de Saúde
            seq += 1
            db.add(Negocio(empresa_id=e.id, vertical="saude", fonte="zeca", id_externo=f"demo-{seq}", status="cancelado",
                           titulo="Saúde coletivo", seguradora="Operadora B",
                           inicio_vigencia=HOJE - timedelta(days=500), fim_vigencia=HOJE - timedelta(days=135)))
        db.commit()

        # E-mails: quem da equipe conversa com essa empresa, com intensidade variável
        intensidade = random.choice([0, 2, 5, 10, 18]) if clientes else random.choice([0, 3])
        msgs, usuarios = [], {u for v in clientes for u in EQUIPE[v]} or {"bruno.rodrigues@innoaseguros.com.br"}
        for k in range(intensidade):
            u = random.choice(sorted(usuarios))
            p = random.choice(pessoas[:2])
            t = AGORA - timedelta(days=random.randint(0, 80), hours=random.randint(0, 9))
            msgs.append({"message_id": f"m{i}-{k}", "thread_id": f"t{i}-{k}", "data": t.isoformat(), "de": u, "para": [p.email]})
            if random.random() < 0.7:
                msgs.append({"message_id": f"r{i}-{k}", "thread_id": f"t{i}-{k}", "data": (t + timedelta(hours=3)).isoformat(),
                             "de": p.email, "para": [u]})
        por_usuario: dict[str, list] = {}
        for m in msgs:
            u = m["de"] if m["de"].endswith("innoaseguros.com.br") else m["para"][0]
            por_usuario.setdefault(u, []).append(m)
        for u, ms in por_usuario.items():
            registrar_mensagens(db, s, u, ms)

    # Um sócio que é cliente PF (Linhas Pessoais) — gera oportunidade no sentido inverso
    socio = Pessoa(nome="Roberto Almeida Figueiredo", nome_normalizado=normalizar_nome_pessoa("Roberto Almeida Figueiredo"),
                   fonte="quiver", cpf="52998224725")
    db.add(socio)
    db.flush()
    db.add(Negocio(pessoa_id=socio.id, vertical="linhas_pessoais", fonte="quiver", id_externo="demo-pf-1", status="ativo",
                   titulo="Auto", produto="Auto", seguradora="Operadora A", fim_vigencia=HOJE + timedelta(days=200), valor=4800))
    alvo = db.query(Empresa).filter(Empresa.nome_normalizado == normalizar_nome_empresa("Indústria Química Delta")).one()
    db.add(Pessoa(nome="Roberto Almeida Figueiredo", nome_normalizado=socio.nome_normalizado, empresa_id=alvo.id,
                  cargo="Sócio-Administrador", senioridade="socio", fonte="receita"))
    db.commit()

    print(recalcular(db, s))


if __name__ == "__main__":
    main()
