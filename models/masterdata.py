from models import db

class MasterData(db.Model):
    __tablename__ = 'masterdata'
    
    id = db.Column(db.Integer, primary_key=True)

    MANCC = db.Column(db.String(10), 
                      db.ForeignKey('nhacungcap.MANCC'), 
                      nullable=False)

    sku = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), nullable=True)

    quantity = db.Column(db.Integer, nullable=False, default=0)

    weight = db.Column(db.Float, nullable=True)
    length = db.Column(db.Float, nullable=True)
    width = db.Column(db.Float, nullable=True)
    height = db.Column(db.Float, nullable=True)

    cbm = db.Column(db.Float, nullable=True)   # Có thể tính tự động

    refix = db.Column(db.String(100), nullable=True)
    remark = db.Column(db.String(255), nullable=True)

    loosecase = db.Column(db.String(100), nullable=True)
    cartonperpallet = db.Column(db.Integer, nullable=True)
    kindpallet = db.Column(db.String(4), nullable=True)

    def __repr__(self):
        return f'<MasterData ID:{self.id} MaNCC:{self.MANCC}>'

    
