"""Popula um banco com dados FICTÍCIOS para demonstração.

Uso: DATABASE_URL=sqlite:///demo.db python scripts/demo.py

Empresas, pessoas, notícias e valores são inventados. As regras (seguro vigente,
tabela, score, to-dos) são as reais da plataforma.
"""

import random
from datetime import date, datetime, timedelta

from crosssell import auth
from crosssell.config import get_settings
from crosssell.connectors.email_m365 import registrar_mensagens
from crosssell.connectors.pipedrive import marcar_saude
from crosssell.db import SessionLocal, init_db
from crosssell.models import Atividade, Empresa, Negocio, Noticia, Pessoa, Usuario
from crosssell.normalize import classificar_senioridade, normalizar_nome_empresa, normalizar_nome_pessoa
from crosssell.pipeline import carregar_usuarios, recalcular

random.seed(11)
HOJE = date.today()
AGORA = datetime.now()

DONO = {1: "victor.boldrini@innoaseguros.com.br", 29: "bruno.rodrigues@innoaseguros.com.br",
        23: "pamela.silva@innoaseguros.com.br", 34: "pedro.acciari@innoaseguros.com.br",
        39: "victor.boldrini@innoaseguros.com.br", 40: "victor.boldrini@innoaseguros.com.br"}
VERT = {1: "linhas_financeiras", 40: "linhas_financeiras", 29: "ramos_elementares", 23: "saude", 34: "saude", 39: None}
ETAPAS = {1: ["Qualificação Lead", "Em Cotação", "Proposta Enviada"], 29: ["Contato Realizado", "Em cotação", "Aguardando fechamento"],
          23: ["Qualificando Lead", "Em cotação / Proposta", "Proposta enviada / Negociação"],
          34: ["Lead Recebido/Sem interação", "Reunião agendada/ligação", "Cotação"],
          39: ["Leads Abordado", "Demonstração Agendada", "Proposta Feita"]}

# nome, CNAE, porte, funcionários, ganhos [(funil, produto, dias até o fim)], abertos [(funil, produto)], temperatura do contato
EMPRESAS = [
    ("Metalúrgica Aurora", "2511000 - Estruturas metálicas", "DEMAIS", 420, [(1, "D&O", 70), (1, "Cyber", -40)], [(29, "Empresarial"), (23, "Saúde")], "muita"),
    ("Transportadora Rota Sul", "4930202 - Transporte de carga", "DEMAIS", 260, [(29, "Transportes", 95)], [(1, "D&O")], "media"),
    ("Clínica Horizonte", "8610101 - Atendimento hospitalar", "DEMAIS", 180, [(1, "E&O Medmal", 210)], [(34, "Saúde")], "muita"),
    ("Grupo Vértice Tecnologia", "6201501 - Desenvolvimento de software", "DEMAIS", 350, [(1, "D&O", 300), (1, "Cyber", 120)], [(29, "Equipamentos")], "media"),
    ("Alimentos Serra Azul", "1091101 - Panificação industrial", "DEMAIS", 610, [(29, "Empresarial", 45)], [(1, "D&O"), (23, "Vida")], "pouca"),
    ("Construtora Pedra Alta", "4120400 - Construção de edifícios", "DEMAIS", 140, [(40, "Garantia", 160)], [(29, "Risco de Engenharia")], "media"),
    ("Laboratório Prisma", "2121101 - Medicamentos", "DEMAIS", 230, [], [(1, "D&O"), (34, "Saúde")], "muita"),
    ("Varejo Bom Preço", "4711302 - Supermercados", "DEMAIS", 900, [(29, "Empresarial", 250)], [(23, "Saúde")], None),
    ("Energia Ventos do Norte", "3511501 - Geração de energia", "DEMAIS", 75, [(1, "D&O", 30), (29, "Equipamentos", 330)], [(23, "Saúde")], "muita"),
    ("Logística Ponto Certo", "5211701 - Armazéns gerais", "DEMAIS", 310, [(29, "Transportes", -60)], [(29, "Transportes"), (1, "Cyber")], "pouca"),
    ("Fintech Ágil Pagamentos", "6619399 - Serviços financeiros", "DEMAIS", 95, [(1, "Cyber", 180)], [(1, "D&O"), (34, "Saúde")], "media"),
    ("Hotel Mar Aberto", "5510801 - Hotéis", "DEMAIS", 160, [], [(29, "Empresarial")], "pouca"),
    ("Indústria Química Delta", "2029100 - Produtos químicos", "DEMAIS", 280, [], [(29, "Empresarial"), (1, "D&O")], None),
    ("Corretora Parceira Sigma", "6622300 - Corretagem de seguros", "EPP", 25, [], [(39, "Canal Cyber")], "media"),
]
NOMES = ["Ana Ribeiro", "Carlos Menezes", "Juliana Prado", "Marcos Teixeira", "Fernanda Lopes", "Rafael Duarte",
         "Patrícia Nogueira", "Eduardo Campos", "Luciana Barros", "Gustavo Pires", "Renata Moraes", "Thiago Rocha",
         "Beatriz Carvalho", "André Fontes", "Camila Freitas", "Rodrigo Sales", "Helena Martins", "Bruno Vieira",
         "Sofia Andrade", "Lucas Ferraz", "Marina Queiroz", "Diego Almeida", "Paula Rezende", "Vinícius Prates",
         "Larissa Couto", "Otávio Nunes", "Débora Siqueira", "Henrique Bastos"]
CARGOS = ["CFO", "Diretora Financeira", "Gerente de RH", "Sócio-Administrador", "Diretor de Operações",
          "Coordenadora de Benefícios", "CEO", "Gerente Administrativo"]
MOTIVOS = {"muita": "Responde no mesmo dia, envia documentos sem precisar pedir e sugeriu uma reunião.",
           "media": "Responde de forma cordial e objetiva, mas só ao que foi perguntado.",
           "pouca": "Respostas curtas; disse que o tema não é prioridade neste trimestre."}
NOTICIAS = ["{n} anuncia expansão com nova unidade no interior de SP", "{n} conclui captação para financiar crescimento",
            "{n} troca diretoria financeira", "{n} é citada entre as empresas que mais crescem no setor"]


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
    master, _ = auth.convidar(db, "rodrigo.pedroni@innoaseguros.com.br", "Rodrigo Pedroni", [], papel="master")
    for i, u in enumerate(db.query(Usuario).all(), start=1):
        u.pipedrive_user_id = i
        if u.senha_hash is None:
            u.senha_hash = "demo"  # aparece como ativo na prévia
    seq = 1000
    for i, (nome, cnae, porte, func, ganhos, abertos, temp) in enumerate(EMPRESAS):
        dominio = normalizar_nome_empresa(nome).replace(" ", "") + ".exemplo.com.br"
        e = Empresa(razao_social=f"{nome} (exemplo)", nome_normalizado=normalizar_nome_empresa(nome), cnpj=cnpj_ficticio(i),
                    dominio=dominio, cnae=cnae, porte=porte, funcionarios=func, cidade="São Paulo", uf="SP",
                    pipedrive_org_id=500 + i, noticias_em=AGORA,
                    funcionarios_fonte="linkedin" if i % 3 != 2 else "pipedrive")
        db.add(e)
        db.flush()
        pessoas = []
        for j in range(2):
            pn = NOMES[(i * 2 + j) % len(NOMES)]
            cargo = CARGOS[(i + j * 3) % len(CARGOS)]
            p = Pessoa(nome=pn, nome_normalizado=normalizar_nome_pessoa(pn), empresa_id=e.id, cargo=cargo,
                       senioridade=classificar_senioridade(cargo), fonte="pipedrive", pipedrive_person_id=7000 + i * 10 + j,
                       email=f"{normalizar_nome_pessoa(pn).split()[0]}@{dominio}")
            db.add(p)
            pessoas.append(p)
        db.flush()
        if temp:
            pessoas[0].temperatura, pessoas[0].temperatura_motivo, pessoas[0].temperatura_em = temp, MOTIVOS[temp], AGORA
        for funil, produto, dias in ganhos:
            seq += 1
            fim = HOJE + timedelta(days=dias)
            db.add(Negocio(empresa=e, pessoa=pessoas[0], vertical=VERT[funil], fonte="pipedrive", id_externo=str(seq),
                           pipeline_id=funil, status="ganho", titulo=f"{produto} {fim.year - 1}", produto=produto,
                           inicio_vigencia=fim - timedelta(days=365), fim_vigencia=fim,
                           valor=round(random.uniform(8e3, 2e5), 2), responsavel_email=DONO[funil]))
        for funil, produto in abertos:
            seq += 1
            db.add(Negocio(empresa=e, pessoa=pessoas[0], vertical=VERT[funil], fonte="pipedrive", id_externo=str(seq),
                           pipeline_id=funil, status="aberto", titulo=f"{produto} {HOJE.year}", produto=produto,
                           etapa=random.choice(ETAPAS[funil]), valor=round(random.uniform(5e3, 1.5e5), 2),
                           responsavel_email=DONO[funil]))
        if i in (0, 7):
            marcar_saude(db, e.id, True)
        for k, modelo in enumerate(random.sample(NOTICIAS, k=random.choice([0, 1, 2]))):
            db.add(Noticia(empresa_id=e.id, titulo=modelo.format(n=nome), fonte="Exemplo", url=f"exemplo-{i}-{k}",
                           publicada_em=AGORA - timedelta(days=random.randint(2, 60))))
        db.commit()

        intensidade = {"muita": 14, "media": 6, "pouca": 2, None: 0}[temp]
        dono = DONO[abertos[0][0]]
        outro = random.choice([u for u in DONO.values() if u != dono])
        msgs = {}
        for k in range(intensidade):
            u = dono if k % 3 else outro
            t = AGORA - timedelta(days=random.randint(0, 70), hours=random.randint(0, 9))
            msgs.setdefault(u, []).append({"message_id": f"m{i}-{k}", "thread_id": f"t{i}-{k}", "data": t.isoformat(),
                                           "de": u, "para": [pessoas[0].email]})
            if temp != "pouca" or k % 2:
                msgs[u].append({"message_id": f"r{i}-{k}", "thread_id": f"t{i}-{k}", "data": (t + timedelta(hours=2)).isoformat(),
                                "de": pessoas[0].email, "para": [u]})
        for u, ms in msgs.items():
            registrar_mensagens(db, s, u, ms)
    db.commit()

    # LinkedIn (o que viria da planilha preenchida pelo n8n)
    empresas = db.query(Empresa).order_by(Empresa.id).all()
    for i, e in enumerate(empresas):
        if i % 3 == 2:
            continue  # parte das empresas ainda não foi lida
        e.linkedin_url = f"https://www.linkedin.com/company/exemplo-{i}"
        e.linkedin_em = AGORA
        for p in e.pessoas:
            p.linkedin_url = f"https://www.linkedin.com/in/exemplo-{p.id}"
            p.linkedin_headline = f"{p.cargo} na {e.razao_social.replace(' (exemplo)', '')}"
            p.linkedin_em = AGORA
    for i, (nome, cargo) in [(0, ("Marta Reis", "Diretora Jurídica")), (3, ("Fábio Monteiro", "CEO")),
                              (6, ("Cláudia Tavares", "Diretora de Pessoas"))]:
        e = empresas[i]
        db.add(Pessoa(nome=nome, nome_normalizado=normalizar_nome_pessoa(nome), empresa_id=e.id, cargo=cargo,
                      senioridade=classificar_senioridade(cargo), fonte="linkedin", linkedin_url=f"https://www.linkedin.com/in/exemplo-dm-{i}",
                      linkedin_headline=f"{cargo} | {e.razao_social.replace(' (exemplo)', '')}", linkedin_em=AGORA))
    mudou = empresas[4].pessoas[0]
    mudou.linkedin_empresa_atual, mudou.linkedin_headline = "Grupo Andorinha", "CFO no Grupo Andorinha"
    db.commit()

    # Atividades da semana (algumas atrasadas e uma feita)
    seg = HOJE - timedelta(days=HOJE.weekday())
    usuarios = {u.email: u for u in db.query(Usuario).all()}
    abertos = db.query(Negocio).filter(Negocio.status == "aberto").order_by(Negocio.id).all()
    planos = [(0, "Ligar para apresentar o Empresarial", "call", -2, False), (1, "Enviar proposta de D&O", "email", 1, False),
              (2, "Reunião de implantação Saúde", "meeting", 3, False), (4, "Cotar D&O com 3 seguradoras", "task", 0, True),
              (9, "Retomar contato sobre Transportes", "call", -4, False), (6, "Apresentar Saúde para o RH", "meeting", 4, False)]
    for idx, assunto, tipo, desloc, feita in planos:
        n = abertos[idx]
        db.add(Atividade(pipedrive_id=None, negocio_id=n.id, pessoa_id=n.pessoa_id, empresa_id=n.empresa_id, assunto=assunto,
                         tipo=tipo, vencimento=min(HOJE + timedelta(days=desloc), seg + timedelta(days=4)) if desloc > 0
                         else HOJE + timedelta(days=desloc),
                         responsavel_id=usuarios[n.responsavel_email].id, criada_por_id=master.id, concluida=feita))
    db.commit()
    print(recalcular(db, s))


if __name__ == "__main__":
    main()
