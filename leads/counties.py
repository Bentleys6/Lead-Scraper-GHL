"""Map a UK postcode to a county/region name for area tags.

Thomson Local's area search is a radius, so a search for "essex" returns
businesses in East London and Suffolk too. Tagging from each business's own
postcode keeps area tags truthful. Postcode areas don't follow county lines
exactly, so districts that straddle a boundary are listed explicitly.
"""

import re

LONDON = "London"

AREAS = {
    # London
    "E": LONDON, "EC": LONDON, "N": LONDON, "NW": LONDON, "SE": LONDON,
    "SW": LONDON, "W": LONDON, "WC": LONDON, "BR": LONDON, "CR": LONDON,
    "HA": LONDON, "SM": LONDON, "UB": LONDON,
    # South East / East
    "AL": "Hertfordshire", "WD": "Hertfordshire", "SG": "Hertfordshire",
    "EN": "Hertfordshire", "HP": "Buckinghamshire", "LU": "Bedfordshire",
    "MK": "Buckinghamshire", "CM": "Essex", "CO": "Essex", "SS": "Essex",
    "RM": "Essex", "IG": "Essex", "IP": "Suffolk", "NR": "Norfolk",
    "CB": "Cambridgeshire", "PE": "Cambridgeshire", "ME": "Kent", "CT": "Kent",
    "TN": "Kent", "DA": "Kent", "BN": "Sussex", "RH": "Sussex", "PO": "Hampshire",
    "SO": "Hampshire", "GU": "Surrey", "KT": "Surrey", "TW": "Surrey",
    "RG": "Berkshire", "SL": "Berkshire", "OX": "Oxfordshire", "NN": "Northamptonshire",
    "SP": "Wiltshire", "SN": "Wiltshire", "BH": "Dorset", "DT": "Dorset",
    # Midlands
    "B": "West Midlands", "CV": "West Midlands", "WV": "West Midlands",
    "WS": "West Midlands", "DY": "West Midlands", "LE": "Leicestershire",
    "NG": "Nottinghamshire", "DE": "Derbyshire", "ST": "Staffordshire",
    "WR": "Worcestershire", "HR": "Herefordshire", "TF": "Shropshire",
    "SY": "Shropshire", "LN": "Lincolnshire",
    # South West
    "BS": "Bristol", "BA": "Somerset", "TA": "Somerset", "EX": "Devon",
    "PL": "Devon", "TQ": "Devon", "TR": "Cornwall", "GL": "Gloucestershire",
    # North
    "M": "Greater Manchester", "OL": "Greater Manchester", "BL": "Greater Manchester",
    "SK": "Greater Manchester", "WN": "Greater Manchester", "L": "Merseyside",
    "CH": "Merseyside", "WA": "Cheshire", "CW": "Cheshire", "PR": "Lancashire",
    "BB": "Lancashire", "FY": "Lancashire", "LA": "Lancashire", "CA": "Cumbria",
    "LS": "West Yorkshire", "BD": "West Yorkshire", "HX": "West Yorkshire",
    "HD": "West Yorkshire", "WF": "West Yorkshire", "S": "South Yorkshire",
    "DN": "South Yorkshire", "HG": "North Yorkshire", "YO": "North Yorkshire",
    "HU": "East Yorkshire", "NE": "Tyne and Wear", "SR": "Tyne and Wear",
    "DH": "County Durham", "DL": "County Durham", "TS": "Teesside",
    # Wales, Scotland, NI
    "CF": "Wales", "NP": "Wales", "SA": "Wales", "LD": "Wales", "LL": "Wales",
    "AB": "Scotland", "DD": "Scotland", "DG": "Scotland", "EH": "Scotland",
    "FK": "Scotland", "G": "Scotland", "HS": "Scotland", "IV": "Scotland",
    "KA": "Scotland", "KW": "Scotland", "KY": "Scotland", "ML": "Scotland",
    "PA": "Scotland", "PH": "Scotland", "TD": "Scotland", "ZE": "Scotland",
    "BT": "Northern Ireland",
}

# Districts that sit on the other side of a county line from their area.
DISTRICTS = {
    "Hertfordshire": ["CM21", "CM22", "CM23", "HP1", "HP2", "HP3", "HP4", "HP23",
                      "EN6", "EN7", "EN8", "EN10", "EN11", "LU6"],
    "Bedfordshire": ["SG15", "SG16", "SG17", "SG18", "SG19", "MK40", "MK41", "MK42",
                     "MK43", "MK44", "MK45", "LU5", "LU7"],
    "Essex": ["EN9", "CB10", "CB11", "CO10"],
    "Suffolk": ["CB8", "CB9", "CO10"],
    LONDON: ["EN1", "EN2", "EN3", "EN4", "EN5", "IG1", "IG2", "IG3", "IG4", "IG5",
             "IG6", "IG11", "RM1", "RM2", "RM3", "RM4", "RM5", "RM6", "RM7", "RM8",
             "RM9", "RM10", "RM11", "RM12", "RM13", "RM14", "DA5", "DA6", "DA7",
             "DA8", "DA14", "DA15", "DA16", "DA17", "DA18", "KT1", "KT2", "KT3",
             "KT4", "KT5", "KT6", "KT9", "TW1", "TW2", "TW3", "TW4", "TW5", "TW6",
             "TW7", "TW8", "TW9", "TW10", "TW11", "TW12", "TW13", "TW14"],
    "Sussex": ["TN2", "TN3", "TN5", "TN6", "TN7", "TN19", "TN20", "TN21", "TN22",
               "TN31", "TN32", "TN33", "TN34", "TN35", "TN36", "TN37", "TN38",
               "TN39", "TN40", "RH10", "RH11", "RH12", "RH13", "RH14", "RH15",
               "RH16", "RH17", "RH18", "RH19", "RH20"],
    "Surrey": ["RH1", "RH2", "RH3", "RH4", "RH5", "RH6", "RH7", "RH8", "RH9"],
    "Buckinghamshire": ["SL0", "SL7", "SL8", "SL9"],
}
DISTRICT_MAP = {d: county for county, ds in DISTRICTS.items() for d in ds}
# CO10 straddles Essex/Suffolk; Sudbury (Suffolk) is its main town.
DISTRICT_MAP["CO10"] = "Suffolk"
# TN2/TN3 are Tunbridge Wells area (Kent), not Sussex.
DISTRICT_MAP["TN2"] = DISTRICT_MAP["TN3"] = "Kent"


def county_for_postcode(postcode):
    """'CM23 2AB' or 'CM232AB' -> 'Hertfordshire'. Returns '' if unknown."""
    pc = re.sub(r"\s+", "", (postcode or "").upper())
    m = re.match(r"^([A-Z]{1,2})(\d[A-Z\d]?)(\d[A-Z]{2})?$", pc)
    if not m:
        return ""
    area, district = m.group(1), m.group(2)
    return DISTRICT_MAP.get(area + district) or AREAS.get(area, "")
