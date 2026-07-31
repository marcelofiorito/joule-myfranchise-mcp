# joule-myfranchise-mcp

MCP Server para o SAP Joule — RunMyFranchise

```
Joule Studio (BTP)  →  MCP Server (CF Python)  →  myfranchise-srv OData V4
       ↑
   LLM do Joule raciocina com os dados retornados
```

## Tools disponíveis

| Tool | Pergunta natural |
|---|---|
| `get_lojas_em_risco` | "Quais lojas têm ruptura de Havaianas no NE em julho?" |
| `get_cobertura_estoque` | "Qual a cobertura de SKU-100 em Recife?" |
| `get_pedidos_pendentes` | "Quantos pedidos de reposição estão aguardando aprovação?" |
| `get_recomendacoes` | "Quais as recomendações ativas para Porto Alegre?" |
| `get_score_rede` | "Qual o score médio das lojas críticas?" |

## Deploy no Cloud Foundry

```bash
# 1. Definir o clientSecret do XSUAA
cf set-env joule-myfranchise-mcp CLIENT_SECRET <xsuaa-clientsecret>

# 2. Definir um token de autenticação para o Joule Studio
cf set-env joule-myfranchise-mcp MCP_AUTH_TOKEN $(uuidgen)

# 3. Deploy
cf push -f manifest-mcp.yml
```

## Integração com Joule Studio

Crie uma BTP Destination (`Connectivity → Destinations`):

| Campo | Valor |
|---|---|
| Name | `RunMyFranchise-MCP` |
| Type | `HTTP` |
| URL | `https://joule-myfranchise-mcp.cfapps.us10.hana.ondemand.com` |
| Authentication | `NoAuthentication` |
| `sap-joule-studio-mcp-server` | `true` |

No Joule Studio: **Adicionar servidor MCP** → selecionar destination `RunMyFranchise-MCP` → caminho `/mcp`.

## Pré-requisitos

- Python 3.12+
- SAP BTP Cloud Foundry
- `cf` CLI instalado
- Credenciais XSUAA do `myfranchise-uaa` (clientid/clientsecret)
