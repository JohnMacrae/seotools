# seotools

Self-hosted SEO tools for dnas.

| Tool | Folder | Port |
|---|---|---|
| Domain Overview (Semrush-style, DataForSEO) | `domain-overview/` | 8095 |

## Install on dnas

```bash
cd /volume1/docker
git clone https://github.com/JohnMacrae/seotools.git
cd seotools/domain-overview
cp .env.example .env      # add DataForSEO login + API password
docker compose up -d --build
```

Open http://dnas:8095

## Update

```bash
cd /volume1/docker/seotools && git pull
cd domain-overview && docker compose up -d --build
```
