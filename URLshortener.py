from fastapi import FastAPI, HTTPException, status, Request, Query
from pydantic import BaseModel
from datetime import datetime, timedelta
import psycopg2
import random
import string
import os
from dotenv import load_dotenv
from typing import Optional, List

load_dotenv()
app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")


class PaginatedURLResponse(BaseModel):
    total_items: int
    page: int
    per_page: int
    items: List[dict]

class URLCreate(BaseModel):
    original_url: str
    short_path: Optional[str] = None
    expires_in_days: Optional[float] = None

class URLUpdate(BaseModel):
    original_url: str

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def generate_short_path():
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for _ in range(6))

# Rotas
@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}

@app.post("/api/urls", status_code=status.HTTP_201_CREATED)
def create_url(data: URLCreate, request: Request):
    
    base_url = str(request.base_url).rstrip('/')
    short_path = data.short_path or generate_short_path()
    expires_at = None

    if data.expires_in_days is not None:  
        expires_at = datetime.now() + timedelta(days=data.expires_in_days)
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT 1 FROM urls WHERE short_path = %s", (short_path,))
        if cur.fetchone():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"O caminho '{short_path}' já está em uso"
            )
        
        cur.execute(
            """INSERT INTO urls (original_url, short_path, expires_at)
               VALUES (%s, %s, %s) RETURNING id, created_at""",
            (data.original_url, short_path, expires_at)
        )
        url_id, created_at = cur.fetchone()
        conn.commit()
        
        return {
            "id": url_id,
            "short_url": f"{base_url}/{short_path}",
            "original_url": data.original_url,
            "created_at": created_at,
            "expires_at": expires_at
        }
        
    except psycopg2.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro no banco de dados: {e}"
        )
    finally:
        if 'conn' in locals():
            conn.close()

@app.put("/api/urls/{short_path}")
def update_url(short_path: str, data: URLUpdate, request: Request):
    base_url = str(request.base_url).rstrip('/')
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            """UPDATE urls SET original_url = %s, updated_at = NOW()
               WHERE short_path = %s RETURNING id, original_url""",
            (data.original_url, short_path)
        )
        result = cur.fetchone()
        
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="URL não encontrada"
            )
        
        conn.commit()
        return {
            "id": result[0],
            "short_url": f"{base_url}/{short_path}",
            "original_url": result[1],
            "updated_at": datetime.now()
        }
    finally:
        if 'conn' in locals():
            conn.close()

@app.get("/{short_path}")
async def redirect_url(short_path: str, request: Request):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, original_url, expires_at 
            FROM urls 
            WHERE short_path = %s
            FOR UPDATE""", (short_path,))
        
        url_data = cur.fetchone()
        
        if not url_data:
            raise HTTPException(status_code=404, detail="URL não encontrada")
        
        url_id, original_url, expires_at = url_data
        
        if expires_at and expires_at < datetime.now():
            raise HTTPException(status_code=410, detail="URL expirada")
        
        cur.execute("""
            INSERT INTO access_logs 
            (url_id, accessed_at, ip_address, user_agent) 
            VALUES (%s, NOW(), %s, %s)
            RETURNING id""",
            (
                url_id,
                request.client.host,
                request.headers.get("user-agent"),
            ))
        
        log_id = cur.fetchone()[0]
        conn.commit()
        
        print(f"Registro de acesso criado - ID: {log_id}, URL: {short_path}")
        
        return {"redirect_to": original_url}
        
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail="Erro interno ao processar o acesso")
    finally:
        if conn:
            conn.close()

@app.get("/api/urls/{short_path}/stats")
def get_stats(short_path: str, request: Request):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        base_url = str(request.base_url).rstrip('/')
        
        cur.execute(
            """SELECT id, original_url FROM urls 
               WHERE short_path = %s""",
            (short_path,)
        )
        url_data = cur.fetchone()
        
        if not url_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="URL não encontrada"
            )
        
        url_id, original_url = url_data
        
        cur.execute(
            """SELECT 
                  COUNT(*) as total_accesses,
                  COUNT(CASE WHEN accessed_at >= NOW() - INTERVAL '30 days' THEN 1 END) as last_30_days
               FROM access_logs 
               WHERE url_id = %s""",
            (url_id,)
        )
        counts = cur.fetchone()
        total_accesses, accesses_last_30_days = counts if counts else (0, 0)
        
        cur.execute(
            """SELECT accessed_at, ip_address, user_agent
               FROM access_logs
               WHERE url_id = %s
               ORDER BY accessed_at DESC
               LIMIT 10""",
            (url_id,)
        )
        access_logs = [{
            "accessed_at": log[0].isoformat() + "Z", 
            "ip_address": log[1],
            "user_agent": log[2]
        } for log in cur.fetchall()]
        
        return {
            "short_url": f"{base_url}/{short_path}",
            "original_url": original_url,
            "total_accesses": total_accesses,
            "accesses_last_30_days": accesses_last_30_days,
            "access_logs": access_logs
        }
        
    except psycopg2.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro no banco de dados: {str(e)}"
        )
    finally:
        if conn:
            conn.close()

@app.delete("/api/urls/{short_path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_url(short_path: str):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM urls WHERE short_path = %s", (short_path,))
        url_data = cur.fetchone()
        
        if not url_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="URL não encontrada"
            )
        
        url_id = url_data[0]
        
        cur.execute("DELETE FROM access_logs WHERE url_id = %s", (url_id,))
        cur.execute("DELETE FROM urls WHERE id = %s", (url_id,))
        conn.commit()
        return None
        
    except psycopg2.Error as e:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro no banco de dados: {e}"
        )
    finally:
        if 'conn' in locals():
            conn.close()

@app.get("/api/urls", response_model=PaginatedURLResponse)
def list_urls(
    request: Request,
    page: int = Query(1, gt=0, description="Número da página"),
    per_page: int = Query(100, gt=0, le=100, description="Itens por página")
):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        base_url = str(request.base_url).rstrip('/')


        cur.execute("SELECT COUNT(*) FROM urls")
        total_items = cur.fetchone()[0]
        offset = (page - 1) * per_page
        cur.execute(
            """SELECT id, original_url, short_path, created_at, expires_at 
               FROM urls 
               ORDER BY created_at DESC
               LIMIT %s OFFSET %s""",
            (per_page, offset)
        )
        
        items = []
        for url in cur.fetchall():
            items.append({
                "id": url[0],
                "original_url": url[1],
                "short_url": f"{base_url}/{url[2]}",
                "created_at": url[3],
                "expires_at": url[4],
                "stats_url": f"{base_url}/api/urls/{url[2]}/stats"
            })

        return {
            "total_items": total_items,
            "page": page,
            "per_page": per_page,
            "items": items
        }

    except psycopg2.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    finally:
        if conn:
            conn.close()