# AgroLink

AgroLink is a Python app where farmers list their produce and traders buy it directly from them, negotiating prices, tracking orders and chatting in one place. It runs as a desktop app or as a website.

## Features

**Farmers**
- Add products with a price per unit, quantity and photo
- Pause, edit and restock listings, with low-stock alerts
- Receive orders and move them through processing, shipped and completed
- Dashboard with sales, top products and ratings

**Traders**
- Search by product, category, farmer or location; sort and filter results
- Save favourite products and farmers
- Order at the listed price or make an offer (the farmer can accept, counter or reject)
- Rate farmers after a completed order

**Everyone**
- Messaging between users and an order progress timeline
- Export orders to CSV (desktop app only)
- Optional "keep me signed in"; passwords are stored as salted hashes

## Run it on your computer

```bash
pip install -r requirements.txt
python agrolink.py
```

Data is saved in JSON files next to the app. They are in `.gitignore`, so accounts and orders are never committed.

## Put it online (Render)

1. Push this repository to GitHub.
2. Sign up at [render.com](https://render.com) with your GitHub account.
3. Click **New +**, then **Blueprint**, pick this repository and click **Apply**. Render reads `render.yaml`, builds the `Dockerfile` and gives you a link like `https://agrolink-xxxx.onrender.com`.

Everything the app saves (accounts, orders, photos) lives on the disk mounted at `/data`. Without that disk, the data is erased every time the service restarts or is redeployed, so keep it. A service with a disk runs a single instance, which is what AgroLink expects.

### Settings (environment variables)

| Variable | Meaning |
| --- | --- |
| `AGROLINK_DATA_DIR` | Folder where data is stored (use the persistent disk, e.g. `/data`) |
| `AGROLINK_WEB` | Set to `1` to run as a website. Also switched on automatically when `PORT` is set |
| `PORT` | Port to listen on (hosts set this for you; default 8000) |

To run the website locally: `AGROLINK_WEB=1 python agrolink.py`, then open http://localhost:8000.

Edit `CURRENCY` near the top of `agrolink.py` to change the currency symbol shown before prices.

## Tech

Python, [Flet](https://flet.dev) (UI, desktop and web), JSON file storage.
