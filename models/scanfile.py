from models import db

class Scanfile(db.Model):
    __tablename__ = 'scanfile'
    id = db.Column(db.Integer, primary_key=True)
    release_key = db.Column(db.String(255), nullable=False)
    sscc = db.Column(db.String(255), nullable=False)
    master_delivery= db.Column(db.String(255), nullable=False)
    qty= db.Column(db.Integer, nullable=False)
    master_ctl=db.Column(db.String(255), nullable=False)
    master_st_company=db.Column(db.String(255), nullable=False)
    master_add1=db.Column(db.String(255), nullable=False)
    master_add2=db.Column(db.String(255), nullable=False)
    master_add3=db.Column(db.String(255), nullable=False)
    master_add4=db.Column(db.String(255), nullable=False)
    ship_to=db.Column(db.String(255), nullable=False)
    st_zip=db.Column(db.String(255), nullable=False)
    barcode=db.Column(db.String(255), nullable=False)
    sku=db.Column(db.String(255), nullable=False)
    jobno=db.Column(db.String(255), nullable=False)
    jobno_type=db.Column(db.String(255), nullable=False)
    tag_label=db.Column(db.String(255), nullable=False)
    pallet=db.Column(db.String(255), nullable=False)
    time_scan=db.Column(db.Date, nullable=False)
    pallet_type=db.Column(db.String(255), nullable=False)
    jobscan=db.Column(db.String(255), nullable=False)
    def __repr__(self):
        return f'<Scanfile ID:{self.id} JobNo:{self.jobno}>'