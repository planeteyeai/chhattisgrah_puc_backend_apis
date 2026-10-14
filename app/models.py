from sqlalchemy import BigInteger, Column, DateTime, Numeric, String, Text

from app.database import Base


class KawardhaData(Base):
    __tablename__ = "kawardha_data"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    district_name = Column(String(255), nullable=False, index=True)
    tehsil_name = Column(String(255), index=True)
    village_name = Column(String(255), index=True)
    village_code = Column(String(64))
    state_name = Column(String(100))
    level = Column(String(32), nullable=False, default="village")
    geom_geojson = Column(Text, nullable=False)
    min_lon = Column(Numeric)
    min_lat = Column(Numeric)
    max_lon = Column(Numeric)
    max_lat = Column(Numeric)
    source = Column(String(255))
    created_at = Column(DateTime, nullable=False)
