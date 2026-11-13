# Phase 6 — Deployment (Run 24/7)

**Goal:** keep ReelDrop running without your laptop being on.
**Time:** 2 days

## Checklist

- [ ] Dockerfile + docker-compose.yml
- [ ] Host chosen
- [ ] Secrets only in `.env` on the host
- [ ] Auto-restart on crash/reboot
- [ ] Weekly backup of `data/`

## Choose a host

| Host | Cost | Good for | Note |
|------|------|----------|------|
| Old laptop / desktop at home | Free | Route B | Must stay on; home IP is safest for Instagram logins |
| Raspberry Pi | ~₹4–6k one time | Route B | Low power, always on |
| Free-tier cloud VM | Free | Routes A & D | Cloud IPs trigger more Instagram security checks for Route B |

## Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
ENV TZ=Asia/Kolkata PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

## docker-compose.yml

```yaml
services:
  scheduler:
    build: .
    command: python main.py
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./config:/app/config
    restart: unless-stopped

  dashboard:
    build: .
    command: streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./config:/app/config
    ports:
      - "8501:8501"
    restart: unless-stopped
```

## Commands

```bash
docker compose up -d --build        # start
docker compose logs -f scheduler    # watch logs
docker compose restart scheduler    # apply schedule changes
docker compose down                 # stop
```

## Backup (Linux cron, every Sunday 2 AM)

```bash
0 2 * * 0  tar czf ~/reeldrop-backup-$(date +\%F).tgz -C ~/reeldrop data config
```

## Security

- Don't expose port 8501 to the whole internet without the password gate; better, access it via a VPN like Tailscale.
- `.env` and `data/session.json` stay on the host only.

✅ **Done when:** ReelDrop survives a reboot and sends on schedule for 7 days without you touching anything except adding reels.
