"""
Database models that pertain to the entire PUDL project.

These tables include many lists of static values, as well as the "glue" that
is required to relate information from different data sources to each other.
"""

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
import pudl.models.entities


###########################################################################
# Tables which represent static lists. E.g. all the US States.
###########################################################################


class State(pudl.models.entities.PUDLBase):
    """A static list of US states."""

    __tablename__ = 'us_states'
    abbr = Column(String, primary_key=True)
    name = Column(String)


class Month(pudl.models.entities.PUDLBase):
    """A list of valid data months."""

    __tablename__ = 'months'
    month = Column(Integer, primary_key=True)


class Quarter(pudl.models.entities.PUDLBase):
    """A list of fiscal/calendar quarters."""

    __tablename__ = 'quarters'
    q = Column(Integer, primary_key=True)  # 1, 2, 3, 4
    end_month = Column(Integer, nullable=False)  # 3, 6, 9, 12


class RTOISO(pudl.models.entities.PUDLBase):
    """Valid Regional Transmission Orgs and Independent System Operators."""

    __tablename__ = 'rto_iso'
    abbr = Column(String, primary_key=True)
    name = Column(String, nullable=False)


class FuelUnit(pudl.models.entities.PUDLBase):
    """A list of strings denoting possible fuel units of measure."""

    __tablename__ = 'fuel_units'
    unit = Column(String, primary_key=True)


class PrimeMover(pudl.models.entities.PUDLBase):
    """A list of strings denoting different types of prime movers."""

    __tablename__ = 'prime_movers'
    prime_mover = Column(String, primary_key="True")


class FERCAccount(pudl.models.entities.PUDLBase):
    """Static list of all the FERC account numbers and descriptions."""

    __tablename__ = 'ferc_accounts'
    id = Column(String, primary_key=True)
    description = Column(String, nullable=False)


class FERCDepreciationLine(pudl.models.entities.PUDLBase):
    """Static list of all the FERC account numbers and descriptions."""

    __tablename__ = 'ferc_depreciation_lines'
    id = Column(String, primary_key=True)
    description = Column(String, nullable=False)


class CensusRegion(pudl.models.entities.PUDLBase):
    """Static list of census regions used by EIA."""

    __tablename__ = 'census_regions'
    abbr = Column(String, primary_key=True)
    name = Column(String, nullable=False)


class NERCRegion(pudl.models.entities.PUDLBase):
    """
    Valid NERC (North American Electric Reliability Corporation) regions.

    As found in the EIA 923 reporting... plus HICC & ASCC for HI & AK.
    """

    __tablename__ = 'nerc_region'
    abbr = Column(String, primary_key=True)
    name = Column(String, nullable=False)


###########################################################################
# "Glue" tables relating names & IDs from different data sources
###########################################################################

class UtilityFERC1(pudl.models.entities.PUDLBase):
    """A FERC respondent -- typically this is a utility company."""

    __tablename__ = 'utilities_ferc'
    utility_id_ferc = Column(Integer, primary_key=True)
    respondent_name = Column(String, nullable=False)
    util_id_pudl = Column(Integer, ForeignKey('utilities.id'), nullable=False)

    util_pudl = relationship("Utility", back_populates="respondents")

    def __repr__(self):
        """Print out a string representation of the UtilityFERC1."""
        return "<UtilityFERC1(respondent_id={}, respondent_name='{}', \
util_id_pudl='{}')>".format(self.utility_id_ferc,
                            self.respondent_name,
                            self.util_id_pudl)


class PlantFERC1(pudl.models.entities.PUDLBase):
    """
    A co-located collection of generation infrastructure.

    Sometimes for a given facility this information is broken out by type of
    plant, depending on the utility and history of the facility. FERC does not
    assign plant IDs -- the only identifying information we have is the name,
    and the respondent it is associated with. The same plant may also be listed
    by multiple utilities (FERC respondents).
    """

    __tablename__ = 'plants_ferc'
    utility_id_ferc = Column(Integer,
                           ForeignKey('utilities_ferc.utility_id_ferc'),
                           primary_key=True)
    plant_name = Column(String, primary_key=True, nullable=False)
    plant_id_pudl = Column(Integer, ForeignKey('plants.id'), nullable=False)

    plants_pudl = relationship("Plant", back_populates="plants_ferc")

    def __repr__(self):
        """Print out a string representation of the PlantFERC1."""
        return "<PlantFERC1(respondent_id={}, plant_name='{}', \
plant_id_pudl={})>".format(self.respondent_id,
                           self.plant_name,
                           self.plant_id_pudl)


class UtilityEIA923(pudl.models.entities.PUDLBase):
    """
    An EIA operator, typically a utility company.

    EIA does assign unique IDs to each operator, as well as supplying a name.
    """

    __tablename__ = 'utilities_eia'
    utility_id_eia = Column(Integer, primary_key=True)
    utility_name = Column(String, nullable=False)
    util_id_pudl = Column(Integer, ForeignKey('utilities.id'), nullable=False)

    util_pudl = relationship("Utility", back_populates="operators")

    def __repr__(self):
        """Print out a string representation of the UtiityEIA923."""
        return "<UtilityEIA923(utility_id_eia={}, utility_name='{}', \
util_id_pudl={})>".format(self.utility_id_eia,
                          self.utility_name,
                          self.util_id_pudl)


class PlantEIA923(pudl.models.entities.PUDLBase):
    """
    A plant listed in the EIA 923 form.

    A single plant typically has only a single operator.  However, plants may
    have multiple owners, and so the same plant may show up under multiple FERC
    respondents (utilities).
    """

    __tablename__ = 'plants_eia'
    plant_id_eia = Column(Integer, primary_key=True)
    plant_name = Column(String, nullable=False)
    plant_id_pudl = Column(Integer, ForeignKey('plants.id'), nullable=False)

    plants_pudl = relationship("Plant", back_populates="plants_eia")

    def __repr__(self):
        """Print out a string representation of the PlantEIA923."""
        return "<PlantEIA923(plant_id_eia={}, plant_name='{}', \
plant_id_pudl={})>".format(self.plant_id_eia,
                           self.plant_name,
                           self.plant_id_pudl)


class Utility(pudl.models.entities.PUDLBase):
    """
    A general electric utility, constructed from FERC, EIA and other data.

    For now this object class is just glue, that allows us to correlate  the
    FERC respondents and EIA operators. In the future it could contain other
    useful information associated with the Utility.  Unfortunately there's not
    a one to one correspondence between FERC respondents and EIA operators, so
    there's some inherent ambiguity in this correspondence.
    """

    __tablename__ = 'utilities'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)

    operators = relationship("UtilityEIA923", back_populates="util_pudl")
    respondents = relationship("UtilityFERC1", back_populates="util_pudl")
    plants = relationship("UtilPlantAssn", back_populates="utilities")

    def __repr__(self):
        """Print out a string representation of the Utility."""
        return "<Utility(id={}, name='{}')>".format(self.id, self.name)


class Plant(pudl.models.entities.PUDLBase):
    """
    A co-located collection of electricity generating infrastructure.

    Plants are enumerated based on their appearing in at least one public data
    source, like the FERC Form 1, or EIA Form 923 reporting.  However, they
    may not appear in all data sources.  Additionally, plants may in some
    cases be broken down into smaller units in one data source than another.
    """

    __tablename__ = 'plants'
    id = Column(Integer, primary_key=True)
    name = Column(String)

    plants_eia = relationship("PlantEIA923", back_populates="plants_pudl")
    plants_ferc = relationship("PlantFERC1", back_populates="plants_pudl")
    utilities = relationship("UtilPlantAssn", back_populates="plants")

    def __repr__(self):
        """Print out a string representation of the Plant."""
        return "<Plant(id={}, name='{}')>".format(self.id, self.name)


class UtilPlantAssn(pudl.models.entities.PUDLBase):
    """Enumerates existence of relationships between plants and utilities."""

    __tablename__ = 'util_plant_assn'
    utility_id = Column(Integer, ForeignKey('utilities.id'), primary_key=True)
    plant_id = Column(Integer, ForeignKey('plants.id'), primary_key=True)

    plants = relationship("Plant", back_populates="utilities")
    utilities = relationship("Utility", back_populates="plants")

    def __repr__(self):
        """Print out a string representation of the UtilPlantAssn."""
        return "<UtilPlantAssn(utiity_id={}, plant_id={})>".\
            format(self.utility_id, self.plant_id)
