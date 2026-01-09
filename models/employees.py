from extensions import db
from sqlalchemy.sql import func

class Employee(db.Model):
    __tablename__ = 'employees'
    id = db.Column(db.Integer, primary_key=True)
    hovaten = db.Column(db.String(255), nullable=False)
    source = db.Column(db.String(100))
    active = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, server_default=func.now())