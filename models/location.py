from extensions import db


class Location(db.Model):
    __tablename__ = 'location'

    id = db.Column(db.Integer, primary_key=True)
    loc_id = db.Column(db.String(50))
    description = db.Column(db.String(100))

    def __repr__(self):
        return f'<Location ID:{self.id} LocID:{self.loc_id}>'
