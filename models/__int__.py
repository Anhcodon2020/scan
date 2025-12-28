from flask_sqlalchemy import SQLAlchemy

# 1. KHỞI TẠO ĐỐI TƯỢNG DB
# Biến 'db' phải được định nghĩa ở đây để app.py có thể import nó.
db = SQLAlchemy()
from .scanfile import Scanfile
from .masterdata import MasterData
from .users import User
from .log import Log
