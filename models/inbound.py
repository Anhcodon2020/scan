from extensions import db

class Inbound(db.Model):
    __tablename__ = 'inbound'
    
    id = db.Column(db.Integer, primary_key=True)
    MANCC = db.Column(db.String(50), nullable=True)
    po = db.Column(db.String(50), nullable=True)
    sku = db.Column(db.String(100), nullable=True)
    carton = db.Column(db.Integer, nullable=True)
    contxe = db.Column(db.String(50), nullable=True)
    datercv = db.Column(db.Date, nullable=True)
    cbm = db.Column(db.Float, nullable=True)
    labour = db.Column(db.String(255), nullable=True)
    PackinglistNo = db.Column(db.String(100), nullable=True)

    def __repr__(self):
        return f'<Inbound ID:{self.id} PO:{self.po}>'