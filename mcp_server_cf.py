#!/usr/bin/env python3
"""
MCP Server — RunMyFranchise Joule (Cloud Foundry)

Padrão: igual ao joule-sfsf-mcp / pocsfsf.
- Destination sem auth (NoAuthentication)
- Token OAuth2 obtido internamente via client_credentials do XSUAA
- TransportSecuritySettings para evitar 421 no CF Go Router

Variáveis de ambiente (cf set-env):
  SRV_URL        = URL do myfranchise-srv
  TOKEN_URL      = https://<tenant>.authentication.us10.hana.ondemand.com/oauth/token
  CLIENT_ID      = clientid do XSUAA
  CLIENT_SECRET  = clientsecret do XSUAA
  PORT           = porta (CF injeta automaticamente)
  MES_REFERENCIA = mês de referência sazonal (padrão: 7)
"""

import os
import json
import time
import datetime
import requests
from mcp.server.fastmcp import FastMCP
try:
    from mcp.server.transport_security import TransportSecuritySettings
    _has_transport_security = True
except ImportError:
    _has_transport_security = False

# ─── Configuração ─────────────────────────────────────────────────
SRV_URL       = os.environ.get("SRV_URL", "http://localhost:4004")
TOKEN_URL     = os.environ.get("TOKEN_URL", "")
CLIENT_ID     = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
MES_REF       = int(os.environ.get("MES_REFERENCIA", "7"))
PORT          = int(os.environ.get("PORT", "8080"))
CF_HOST       = os.environ.get("CF_HOST", "joule-myfranchise-mcp.cfapps.us10.hana.ondemand.com")

# ─── Token cache ──────────────────────────────────────────────────
_token_cache: dict = {}

def get_token() -> str:
    """Obtém Bearer token via XSUAA client_credentials (com cache)."""
    now = time.time()
    if _token_cache.get("expires_at", 0) - 30 > now:
        return _token_cache["token"]
    if not TOKEN_URL or not CLIENT_ID or not CLIENT_SECRET:
        return ""
    try:
        r = requests.post(
            TOKEN_URL,
            auth=(CLIENT_ID, CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        token = r.json().get("access_token", "") if r.status_code == 200 else ""
        _token_cache["token"]      = token
        _token_cache["expires_at"] = now + 3500
        return token
    except Exception:
        return ""

def odata(path: str, params: dict = None) -> dict:
    """Chama a OData API do myfranchise-srv com token Bearer."""
    token = get_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{SRV_URL}/franqueadora/{path}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

# ─── FastMCP ──────────────────────────────────────────────────────
_mcp_kwargs = {
    "instructions": (
        "Você é o assistente da rede de franquias RunMyFranchise. "
        "Use as ferramentas para responder sobre estoque, ruptura, "
        "recomendações da IA e score de saúde das lojas. "
        "Considere sazonalidade: Havaianas em julho têm demanda 1,8x no NE e 0,4x no Sul."
    ),
}
if _has_transport_security:
    _mcp_kwargs["transport_security"] = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[CF_HOST, "localhost:*", "127.0.0.1:*", "*.cfapps.us10.hana.ondemand.com"],
    )

mcp = FastMCP("RunMyFranchise Joule", **_mcp_kwargs)

# ─── TOOL 1: lojas em risco de ruptura ───────────────────────────
@mcp.tool()
def get_lojas_em_risco(regiao: str = "", categoria: str = "") -> str:
    """
    Lista lojas com risco de ruptura de estoque (cobertura < lead time), considerando
    sazonalidade regional. Filtre por regiao (NE, S, SE, CO, N) e/ou categoria (ex: Sandálias).
    Exemplo: get_lojas_em_risco(regiao='NE', categoria='Sandálias')
    """
    try:
        data  = odata("Estoque_Unidade", {
            "$select": "unidade_ID,unidadeNome,unidadeCidade,regiaoCode,sku,nomeProduto,categoria,saldoAtual,coberturaDias,leadTimeDias,estoqueCriticality",
            "$top": "200",
        })
        items = data.get("value", [])
        if regiao:
            items = [i for i in items if i.get("regiaoCode") == regiao]
        if categoria:
            items = [i for i in items if (i.get("categoria") or "").lower() == categoria.lower()]
        items = [i for i in items if int(i.get("estoqueCriticality") or 3) < 3]
        items.sort(key=lambda i: float(i.get("coberturaDias") or 999))

        if not items:
            return json.dumps({"total": 0, "mensagem": "Nenhuma loja em risco com esses filtros.", "mes_referencia": MES_REF})

        return json.dumps({
            "total": len(items),
            "mes_referencia": MES_REF,
            "lojas": [{
                "loja":         i.get("unidadeNome"),
                "cidade":       i.get("unidadeCidade"),
                "regiao":       i.get("regiaoCode"),
                "produto":      i.get("nomeProduto"),
                "saldo":        i.get("saldoAtual"),
                "coberturaDias": i.get("coberturaDias"),
                "leadTime":     i.get("leadTimeDias"),
                "criticidade":  "RUPTURA IMINENTE" if int(i.get("estoqueCriticality") or 3) == 1 else "ATENÇÃO",
            } for i in items]
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 2: cobertura de SKU numa loja ──────────────────────────
@mcp.tool()
def get_cobertura_estoque(unidade_id: str, sku: str = "") -> str:
    """
    Retorna cobertura de estoque em dias de uma loja, com sazonalidade aplicada.
    unidade_id: ID da loja (ex: u178). sku: código do SKU (opcional).
    Exemplo: get_cobertura_estoque(unidade_id='u178', sku='SKU-100')
    """
    try:
        data  = odata("Estoque_Unidade", {
            "$select": "unidade_ID,unidadeNome,unidadeCidade,regiaoCode,sku,nomeProduto,saldoAtual,coberturaDias,leadTimeDias,estoqueCriticality",
            "$top": "200",
        })
        items = [i for i in data.get("value", []) if i.get("unidade_ID") == unidade_id]
        if sku:
            items = [i for i in items if i.get("sku") == sku]
        if not items:
            return json.dumps({"erro": f"Nenhum item encontrado para {unidade_id}"})
        u = items[0]
        return json.dumps({
            "loja":    u.get("unidadeNome"),
            "cidade":  u.get("unidadeCidade"),
            "regiao":  u.get("regiaoCode"),
            "mes_referencia": MES_REF,
            "itens": [{
                "sku":          i.get("sku"),
                "produto":      i.get("nomeProduto"),
                "saldo":        i.get("saldoAtual"),
                "coberturaDias": i.get("coberturaDias"),
                "leadTime":     i.get("leadTimeDias"),
                "status":       "RUPTURA IMINENTE" if int(i.get("estoqueCriticality") or 3) == 1 else "ATENÇÃO" if int(i.get("estoqueCriticality") or 3) == 2 else "OK",
            } for i in items]
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 3: pedidos pendentes ────────────────────────────────────
@mcp.tool()
def get_pedidos_pendentes(unidade_id: str = "") -> str:
    """
    Lista pedidos de reposição aguardando aprovação (status PENDENTE).
    unidade_id: filtrar por loja (opcional).
    """
    try:
        data  = odata("Pedidos_Reposicao", {"$top": "100"})
        items = [i for i in data.get("value", []) if i.get("status_code") == "PENDENTE"]
        if unidade_id:
            items = [i for i in items if i.get("unidade_ID") == unidade_id]

        unids = {u["ID"]: u for u in odata("Unidades", {"$select": "ID,nome,cidade", "$top": "100"}).get("value", [])}

        return json.dumps({
            "total": len(items),
            "pedidos": [{
                "loja":        unids.get(i.get("unidade_ID"), {}).get("nome", i.get("unidade_ID")),
                "cidade":      unids.get(i.get("unidade_ID"), {}).get("cidade"),
                "produto":     i.get("nomeProduto"),
                "sku":         i.get("sku"),
                "qtdSugerida": i.get("qtdSugerida"),
                "fornecedor":  i.get("fornecedorSugerido"),
                "prazo":       i.get("prazoDesejado"),
                "justificativa": i.get("justificativa"),
            } for i in items]
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 4: recomendações da IA ─────────────────────────────────
@mcp.tool()
def get_recomendacoes(unidade_id: str = "", prioridade: str = "") -> str:
    """
    Retorna recomendações geradas pelo gpt-4o para as lojas.
    unidade_id: ID da loja (ex: u147, opcional). prioridade: ALTA, MEDIA, BAIXA (opcional).
    """
    try:
        data  = odata("Recomendacoes", {"$top": "100"})
        items = [i for i in data.get("value", []) if i.get("status_code") == "NOVA"]
        if unidade_id:
            items = [i for i in items if i.get("unidade_ID") == unidade_id]
        if prioridade:
            items = [i for i in items if i.get("prioridade_code") == prioridade.upper()]

        unids = {u["ID"]: u for u in odata("Unidades", {"$select": "ID,nome,cidade", "$top": "100"}).get("value", [])}

        return json.dumps({
            "total": len(items),
            "recomendacoes": [{
                "loja":      unids.get(i.get("unidade_ID"), {}).get("nome", i.get("unidade_ID")),
                "tipo":      i.get("tipo_code"),
                "prioridade": i.get("prioridade_code"),
                "titulo":    i.get("titulo"),
                "descricao": i.get("descricao"),
            } for i in items]
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"erro": str(e)})

# ─── TOOL 5: score de saúde ───────────────────────────────────────
@mcp.tool()
def get_score_rede(regiao: str = "", somente_criticas: bool = False) -> str:
    """
    Retorna o score de saúde das lojas da rede.
    regiao: NE, S, SE, CO, N (opcional). somente_criticas: True para só lojas críticas.
    """
    try:
        data  = odata("Saude_Dashboard", {"$top": "100"})
        all_  = data.get("value", [])
        items = list(all_)
        if regiao:
            items = [i for i in items if i.get("regiao_code") == regiao]
        if somente_criticas:
            items = [i for i in items if int(i.get("scoreCriticality") or 3) == 1]
        items.sort(key=lambda i: float(i.get("scoreSaude") or 100))

        return json.dumps({
            "resumo_rede": {
                "total":     len(all_),
                "criticas":  sum(1 for i in all_ if int(i.get("scoreCriticality") or 3) == 1),
                "atencao":   sum(1 for i in all_ if int(i.get("scoreCriticality") or 3) == 2),
                "saudaveis": sum(1 for i in all_ if int(i.get("scoreCriticality") or 3) == 3),
                "scoreMedia": round(sum(float(i.get("scoreSaude") or 0) for i in all_) / len(all_), 1) if all_ else 0,
            },
            "lojas": [{
                "loja":       i.get("nome"),
                "cidade":     i.get("cidade"),
                "regiao":     i.get("regiao_code"),
                "cluster":    i.get("cluster_code"),
                "score":      i.get("scoreSaude"),
                "criticidade": "CRÍTICO" if int(i.get("scoreCriticality") or 3) == 1 else "ATENÇÃO" if int(i.get("scoreCriticality") or 3) == 2 else "SAUDÁVEL",
            } for i in items[:20]]
        }, ensure_ascii=False, indent=2)
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
                    "status": "UP", "service": "joule-myfranchise-mcp", "version": "1.0.0",
                    "tools": ["get_lojas_em_risco","get_cobertura_estoque","get_pedidos_pendentes","get_recomendacoes","get_score_rede"],
                    "mes_referencia": MES_REF,
                })
            return await call_next(request)

    app = mcp_app
    app = HealthMiddleware(app)
    app = TrustedHostMiddleware(app, allowed_hosts=["*"])

    uvicorn.run(app, host="0.0.0.0", port=PORT, forwarded_allow_ips="*", proxy_headers=True)
