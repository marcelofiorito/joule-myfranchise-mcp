#!/usr/bin/env python3
"""
MCP Server — RunMyFranchise Joule (Cloud Foundry / SSE transport)

Expõe ferramentas para o SAP Joule consultar dados da rede de franquias.
Conecta ao backend CAP via OData V4 (/franqueadora) com token client_credentials.

Variáveis de ambiente obrigatórias (cf set-env):
  SRV_URL         = URL do myfranchise-srv (ex: https://sa-build-platform-org-dev-myfranchise-srv.cfapps.us10.hana.ondemand.com)
  TOKEN_URL       = URL do token XSUAA (ex: https://<tenant>.authentication.us10.hana.ondemand.com/oauth/token)
  CLIENT_ID       = clientid do XSUAA (sb-myfranchise-DEV!t597567)
  CLIENT_SECRET   = clientsecret do XSUAA (cf set-env joule-myfranchise-mcp CLIENT_SECRET <secret>)
  MCP_AUTH_TOKEN  = token secreto para autenticar o Joule Studio (defina um UUID)

Variáveis opcionais:
  PORT            = porta HTTP (padrão: 8080, CF injeta automaticamente)
  MES_REFERENCIA  = mês de referência para sazonalidade (padrão: 7)
"""

import os
import json
import requests
from mcp.server.fastmcp import FastMCP

# ─── Configuração ─────────────────────────────────────────────────
SRV_URL        = os.environ.get("SRV_URL", "http://localhost:4004")
TOKEN_URL      = os.environ.get("TOKEN_URL", "")
CLIENT_ID      = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET  = os.environ.get("CLIENT_SECRET", "")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
MES_REF        = int(os.environ.get("MES_REFERENCIA", "7"))
PORT           = int(os.environ.get("PORT", "8080"))

# ─── Token cache ──────────────────────────────────────────────────
_token_cache: dict = {}

def get_token() -> str:
    """Obtém token client_credentials do XSUAA (com cache simples)."""
    import time
    now = time.time()
    if _token_cache.get("token") and _token_cache.get("expires", 0) > now + 60:
        return _token_cache["token"]

    if not TOKEN_URL or not CLIENT_ID or not CLIENT_SECRET:
        return ""  # modo dev local sem auth

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"]   = data["access_token"]
    _token_cache["expires"] = now + data.get("expires_in", 3600)
    return _token_cache["token"]

def odata(path: str, params: dict = None) -> dict:
    """Chama a OData API do myfranchise-srv com token."""
    headers = {"Accept": "application/json"}
    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{SRV_URL}/franqueadora/{path}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

# ─── FastMCP Server ───────────────────────────────────────────────
mcp = FastMCP(
    "RunMyFranchise Joule",
    instructions=(
        "Você é o assistente inteligente da rede de franquias RunMyFranchise. "
        "Use as ferramentas disponíveis para responder perguntas sobre estoque, "
        "risco de ruptura, recomendações da IA, score de saúde e pedidos de reposição. "
        "Considere sempre a sazonalidade regional: Havaianas em julho têm demanda "
        "muito maior no Nordeste do que no Sul. Apresente números de forma clara."
    ),
)

# ─── TOOL 1: lojas em risco de ruptura ───────────────────────────
@mcp.tool()
def get_lojas_em_risco(
    regiao: str = "",
    categoria: str = "",
) -> str:
    """
    Lista lojas com risco de ruptura de estoque, considerando sazonalidade regional.
    Use regiao para filtrar (NE, S, SE, CO, N). Use categoria para filtrar por produto (ex: Sandálias).
    Retorna cobertura em dias, fator sazonal e criticidade (RUPTURA IMINENTE ou ATENÇÃO).
    Exemplo: get_lojas_em_risco(regiao='NE', categoria='Sandálias')
    """
    try:
        params = {"$filter": "estoqueCriticality lt 3"}
        if regiao:
            params["$filter"] += f" and regiaoCode eq '{regiao}'"
        if categoria:
            params["$filter"] += f" and categoria eq '{categoria}'"
        params["$select"] = "unidadeNome,unidadeCidade,regiaoCode,sku,nomeProduto,saldoAtual,coberturaDias,leadTimeDias,estoqueCriticality,status_code"
        params["$orderby"] = "coberturaDias asc"
        params["$top"] = "20"

        data = odata("Estoque_Unidade", params)
        items = data.get("value", [])

        if not items:
            return json.dumps({"total": 0, "mensagem": "Nenhuma loja em risco encontrada com os filtros informados.", "mes_referencia": MES_REF})

        lojas = [{
            "loja":         i.get("unidadeNome"),
            "cidade":       i.get("unidadeCidade"),
            "regiao":       i.get("regiaoCode"),
            "sku":          i.get("sku"),
            "produto":      i.get("nomeProduto"),
            "saldo":        i.get("saldoAtual"),
            "coberturaDias": i.get("coberturaDias"),
            "leadTime":     i.get("leadTimeDias"),
            "criticidade":  "RUPTURA IMINENTE" if i.get("estoqueCriticality") == 1 else "ATENÇÃO",
        } for i in items]

        return json.dumps({"total": len(lojas), "mes_referencia": MES_REF, "lojas": lojas}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 2: cobertura de um SKU numa loja ────────────────────────
@mcp.tool()
def get_cobertura_estoque(
    unidade_id: str,
    sku: str = "",
) -> str:
    """
    Retorna a cobertura de estoque em dias de uma loja, com sazonalidade regional aplicada.
    unidade_id: ID da loja (ex: u178). sku: código do SKU (ex: SKU-100), opcional.
    Exemplo: get_cobertura_estoque(unidade_id='u178', sku='SKU-100')
    """
    try:
        filt = f"unidade_ID eq '{unidade_id}'"
        if sku:
            filt += f" and sku eq '{sku}'"
        data = odata("Estoque_Unidade", {
            "$filter": filt,
            "$select": "unidadeNome,unidadeCidade,regiaoCode,sku,nomeProduto,saldoAtual,coberturaDias,leadTimeDias,estoqueCriticality",
        })
        items = data.get("value", [])
        if not items:
            return json.dumps({"erro": f"Nenhum item encontrado para {unidade_id}"})

        u = items[0]
        itens = [{
            "sku":          i.get("sku"),
            "produto":      i.get("nomeProduto"),
            "saldo":        i.get("saldoAtual"),
            "coberturaDias": i.get("coberturaDias"),
            "leadTime":     i.get("leadTimeDias"),
            "status":       "RUPTURA IMINENTE" if i.get("estoqueCriticality") == 1 else "ATENÇÃO" if i.get("estoqueCriticality") == 2 else "OK",
        } for i in items]

        return json.dumps({
            "loja":    u.get("unidadeNome"),
            "cidade":  u.get("unidadeCidade"),
            "regiao":  u.get("regiaoCode"),
            "mes_referencia": MES_REF,
            "itens":   itens,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 3: pedidos de reposição pendentes ────────────────────────
@mcp.tool()
def get_pedidos_pendentes(
    unidade_id: str = "",
    status: str = "PENDENTE",
) -> str:
    """
    Lista pedidos de reposição aguardando aprovação (status PENDENTE por padrão).
    unidade_id: filtrar por loja (opcional). status: PENDENTE, APROVADO, RECUSADO, ENVIADO, RECEBIDO.
    Exemplo: get_pedidos_pendentes() ou get_pedidos_pendentes(unidade_id='u178')
    """
    try:
        filt = f"status_code eq '{status}'"
        if unidade_id:
            filt += f" and unidade_ID eq '{unidade_id}'"
        data = odata("Pedidos_Reposicao", {
            "$filter": filt,
            "$select": "unidade_ID,sku,nomeProduto,qtdSugerida,fornecedorSugerido,prazoDesejado,status_code,origem,justificativa",
            "$orderby": "createdAt desc",
        })
        items = data.get("value", [])

        # Enriquecer com nomes das unidades
        unidades_data = odata("Unidades", {"$select": "ID,nome,cidade"})
        um = {u["ID"]: u for u in unidades_data.get("value", [])}

        pedidos = [{
            "loja":          um.get(i.get("unidade_ID"), {}).get("nome", i.get("unidade_ID")),
            "cidade":        um.get(i.get("unidade_ID"), {}).get("cidade"),
            "sku":           i.get("sku"),
            "produto":       i.get("nomeProduto"),
            "qtdSugerida":   i.get("qtdSugerida"),
            "fornecedor":    i.get("fornecedorSugerido"),
            "prazo":         i.get("prazoDesejado"),
            "status":        i.get("status_code"),
            "origem":        i.get("origem"),
            "justificativa": i.get("justificativa"),
        } for i in items]

        return json.dumps({"total": len(pedidos), "status": status, "pedidos": pedidos}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 4: recomendações da IA ──────────────────────────────────
@mcp.tool()
def get_recomendacoes(
    unidade_id: str = "",
    prioridade: str = "",
) -> str:
    """
    Retorna recomendações geradas pelo gpt-4o para lojas da rede.
    unidade_id: ID da loja (ex: u147), opcional. prioridade: ALTA, MEDIA, BAIXA, opcional.
    Exemplo: get_recomendacoes(unidade_id='u147') ou get_recomendacoes(prioridade='ALTA')
    """
    try:
        filt = "status_code eq 'NOVA'"
        if unidade_id:
            filt += f" and unidade_ID eq '{unidade_id}'"
        if prioridade:
            filt += f" and prioridade_code eq '{prioridade}'"
        data = odata("Recomendacoes", {
            "$filter": filt,
            "$select": "unidade_ID,tipo_code,titulo,descricao,prioridade_code,status_code,dataGeracao",
            "$orderby": "prioridade_code asc",
        })
        items = data.get("value", [])

        unidades_data = odata("Unidades", {"$select": "ID,nome,cidade"})
        um = {u["ID"]: u for u in unidades_data.get("value", [])}

        recs = [{
            "loja":      um.get(i.get("unidade_ID"), {}).get("nome", i.get("unidade_ID")),
            "cidade":    um.get(i.get("unidade_ID"), {}).get("cidade"),
            "tipo":      i.get("tipo_code"),
            "prioridade": i.get("prioridade_code"),
            "titulo":    i.get("titulo"),
            "descricao": i.get("descricao"),
            "geradaEm":  i.get("dataGeracao"),
        } for i in items]

        return json.dumps({"total": len(recs), "recomendacoes": recs}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 5: score de saúde da rede ───────────────────────────────
@mcp.tool()
def get_score_rede(
    regiao: str = "",
    cluster: str = "",
    somente_criticas: bool = False,
) -> str:
    """
    Retorna o score de saúde das lojas da rede com resumo e detalhes.
    regiao: NE, S, SE, CO, N (opcional). cluster: STD, EXP, FLG (opcional).
    somente_criticas: se True, retorna apenas lojas com score crítico (vermelho).
    Exemplo: get_score_rede(regiao='NE') ou get_score_rede(somente_criticas=True)
    """
    try:
        filt_parts = []
        if regiao:
            filt_parts.append(f"regiao_code eq '{regiao}'")
        if cluster:
            filt_parts.append(f"cluster_code eq '{cluster}'")
        if somente_criticas:
            filt_parts.append("scoreCriticality eq 1")

        params = {
            "$select": "nome,cidade,regiao_code,cluster_code,scoreSaude,compliancePct,performancePct,qtdAlertasAlta,scoreCriticality",
            "$orderby": "scoreSaude asc",
            "$top": "20",
        }
        if filt_parts:
            params["$filter"] = " and ".join(filt_parts)

        data    = odata("Saude_Dashboard", params)
        all_data = odata("Saude_Dashboard", {"$select": "scoreSaude,scoreCriticality"})
        all_items = all_data.get("value", [])
        items    = data.get("value", [])

        resumo = {
            "total":     len(all_items),
            "criticas":  sum(1 for i in all_items if i.get("scoreCriticality") == 1),
            "atencao":   sum(1 for i in all_items if i.get("scoreCriticality") == 2),
            "saudaveis": sum(1 for i in all_items if i.get("scoreCriticality") == 3),
            "scoreMedia": round(sum(float(i.get("scoreSaude", 0)) for i in all_items) / len(all_items), 1) if all_items else 0,
        }

        lojas = [{
            "loja":       i.get("nome"),
            "cidade":     i.get("cidade"),
            "regiao":     i.get("regiao_code"),
            "cluster":    i.get("cluster_code"),
            "score":      i.get("scoreSaude"),
            "compliance": i.get("compliancePct"),
            "performance": i.get("performancePct"),
            "alertasAlta": i.get("qtdAlertasAlta"),
            "criticidade": "CRÍTICO" if i.get("scoreCriticality") == 1 else "ATENÇÃO" if i.get("scoreCriticality") == 2 else "SAUDÁVEL",
        } for i in items]

        return json.dumps({"resumo_rede": resumo, "lojas": lojas}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── Entrypoint HTTP para Cloud Foundry ──────────────────────────
if __name__ == "__main__":
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse

    print(f"✅ joule-myfranchise-mcp iniciando na porta {PORT}")

    mcp_app = mcp.streamable_http_app()

    class HealthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            if request.url.path == "/health":
                return JSONResponse({
                    "status": "UP",
                    "service": "joule-myfranchise-mcp",
                    "version": "1.0.0",
                    "tools": ["get_lojas_em_risco", "get_cobertura_estoque",
                              "get_pedidos_pendentes", "get_recomendacoes", "get_score_rede"],
                    "mes_referencia": MES_REF,
                })
            return await call_next(request)

    app = mcp_app
    app = HealthMiddleware(app)

    # TrustedHostMiddleware: obrigatório no CF — o Go Router faz proxy reverso
    # e o Host header pode causar 421 sem este middleware.
    app = TrustedHostMiddleware(app, allowed_hosts=["*"])

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        forwarded_allow_ips="*",
        proxy_headers=True,
    )
