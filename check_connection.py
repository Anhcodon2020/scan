import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
from config import Config

def check_db_connection():
    print("--- Bắt đầu kiểm tra kết nối Database ---")
    
    # Lấy URI từ Config
    db_uri = Config.SQLALCHEMY_DATABASE_URI
    # Ẩn mật khẩu khi in ra log để bảo mật
    safe_uri = db_uri.split('@')[-1] if '@' in db_uri else "..."
    driver = db_uri.split('://')[0]
    print(f"Target: {safe_uri} | Driver: {driver}")
    
    # Lấy Engine Options (bao gồm cấu hình SSL ca.pem)
    engine_opts = getattr(Config, 'SQLALCHEMY_ENGINE_OPTIONS', {})
    
    try:
        engine = create_engine(db_uri, **engine_opts)
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            print("✅ KẾT NỐI AIVEN MYSQL THÀNH CÔNG! (SSL Verified)")
            
    except Exception as e:
        print("❌ KẾT NỐI THẤT BẠI!")
        print(f"Chi tiết lỗi: {e}")

if __name__ == "__main__":
    check_db_connection()