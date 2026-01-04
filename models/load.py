from extensions import db
from datetime import datetime

class Load(db.Model):
    __tablename__ = 'load'
    id = db.Column(db.Integer, primary_key=True)
    jobno_type = db.Column(db.String(255), nullable=False)
    pallet_no = db.Column(db.String(255), nullable=False)
    pallet_type = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    status = db.Column(db.String(50), default='PENDING') # PENDING: Chờ in, PRINTED: Đã in
    created_by = db.Column(db.String(255))

    def __repr__(self):
        return f'<Load {self.id} Pallet:{self.pallet_no}>'