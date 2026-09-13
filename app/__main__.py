import os
import logging
from datetime import datetime, timedelta, timezone

import uvicorn
from dotenv import load_dotenv


class ShanghaiFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def main():
    load_dotenv()
    handler = logging.StreamHandler()
    handler.setFormatter(ShanghaiFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")), workers=1, log_config=None)


if __name__ == "__main__":
    main()
