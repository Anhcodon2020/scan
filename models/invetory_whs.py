from extensions import db


class InventoryWhs(db.Model):
    __tablename__ = 'invetory_whs'

    id = db.Column(db.Integer, primary_key=True)
    loc_id = db.Column(db.Integer, db.ForeignKey('location.id'))
    sku = db.Column(db.String(100))
    pallet = db.Column(db.Integer)
    carton = db.Column(db.Integer)
    time_update = db.Column(db.DateTime)

    location = db.relationship('Location', backref='inventory_items')

    def __repr__(self):
        return f'<InventoryWhs ID:{self.id} SKU:{self.sku}>'
