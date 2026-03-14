from extensions import db
from sqlalchemy.sql import func

class LaborAssignment(db.Model):
    __tablename__ = 'labor_assignment'
    
    id = db.Column(db.Integer, primary_key=True)
    inbound_id = db.Column(db.Integer, db.ForeignKey('inbound.id'), nullable=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=True)
    work_date = db.Column(db.Date, nullable=True)
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now())
    packinglist_no = db.Column(db.String(50), nullable=True)
    # Thiết lập quan hệ để dễ dàng truy xuất dữ liệu liên quan
    inbound = db.relationship('Inbound', backref='assignments')
    employee = db.relationship('Employee', backref='assignments')

    def __repr__(self):
        return f'<LaborAssignment ID:{self.id} Date:{self.work_date}>'