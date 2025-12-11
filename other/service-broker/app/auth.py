from fastapi import Header, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import os

security = HTTPBasic()

API_VERSION = "2.12"

def api_version_check(x_broker_api_version: str = Header(...)):
    if x_broker_api_version != API_VERSION:
        raise HTTPException(
            status_code=412,
            detail=f"Unsupported API version. Use {API_VERSION}"
        )

def basic_auth(credentials: HTTPBasicCredentials = Depends(security)):
    user = os.getenv("BASIC_AUTH_USER")
    pwd = os.getenv("BASIC_AUTH_PASS")

    if credentials.username != user or credentials.password != pwd:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return True
