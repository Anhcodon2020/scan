from extensions import db


class InventoryWhs(db.Model):
    __tablename__ = 'inventory_whs'

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50))
    loc_id = db.Column(db.String(50))
    qty = db.Column(db.Integer, default=0)
    sub_loc = db.Column(db.Integer)
    date_update = db.Column(db.DateTime)

    def __repr__(self):
        return f'<InventoryWhs ID:{self.id} SKU:{self.sku}>'
