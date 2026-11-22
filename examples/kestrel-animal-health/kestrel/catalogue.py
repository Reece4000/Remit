"""What Kestrel sells, who makes it, and who buys it: the data a year of history is made of.

The products are the kinds a veterinary exporter carries (vaccines, diagnostic kits,
laboratory reagents and plastics, instruments and cold chain equipment) with what each
costs in pounds, what it weighs packed for export, how it must be kept and how long it
keeps. Diseases, commodity codes, UN numbers, countries, cities and airports are real.
Every organisation and person is invented, and every email address is on the reserved
`.example` domain; the only telephone numbers are in Ofcom's ranges kept for drama.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .common import PaymentTerms
from .month import PARTIES, Contact, Organisation
from .stock import Storage


class Family(StrEnum):
    """A kind of product, which decides who makes it and how it travels."""

    VACCINE = "vaccine"
    ELISA = "elisa"
    PCR = "pcr"
    RAPID = "rapid"
    REAGENT = "reagent"
    CONSUMABLE = "consumable"
    EQUIPMENT = "equipment"
    COLD_CHAIN = "cold_chain"


@dataclass(frozen=True)
class Product:
    """Something Kestrel sells, as it is added to the items, and how it behaves."""

    code: str
    name: str
    family: Family
    unit: str
    cost: Decimal
    """What one unit costs to buy, in pounds, before a manufacturer's own pricing."""
    weight: Decimal
    """What one unit weighs packed for export, in kilograms."""
    storage: Storage
    shelf_life: int | None
    """How many days a batch keeps from manufacture; None for what does not expire."""
    dangerous: str | None
    """Its UN number and class when it is dangerous goods by air; UN3373 alone for a
    biological substance, category B."""
    commodity: str
    """Its UK commodity code, which the customs declaration needs."""

    @property
    def description(self) -> str:
        kept = {
            Storage.CHILLED: " Store at 2 to 8 °C.",
            Storage.FROZEN: " Store at -20 °C or colder.",
            Storage.AMBIENT: "",
        }[self.storage]
        hazard = f" Dangerous goods: {self.dangerous}." if self.dangerous else ""
        return f"Commodity code {self.commodity}.{kept}{hazard}"


@dataclass(frozen=True)
class Maker:
    """A manufacturer Kestrel buys from: who it is, what it makes and how it trades."""

    organisation: Organisation
    currency: str
    families: tuple[Family, ...]
    lead_time: tuple[int, int]
    """The fewest and most days it takes to supply."""
    share: int
    """How much of its families' range it makes, against the others making them."""

    @property
    def code(self) -> str:
        return self.organisation.code


@dataclass(frozen=True)
class Buyer:
    """A customer, and how it buys."""

    organisation: Organisation
    city: str
    airport: str
    currency: str
    terms: PaymentTerms

    @property
    def code(self) -> str:
        return self.organisation.code


def _vaccines() -> list[Product]:
    # Code, disease and kind, doses per vial, pounds per dose, live (shorter shelf life).
    table = (
        ("NDL", "Newcastle disease vaccine, live, La Sota strain", (500, 2500, 5000), "0.012", 1),
        ("NDI", "Newcastle disease vaccine, inactivated, oil emulsion", (500, 1000), "0.035", 0),
        ("IBH", "Infectious bronchitis vaccine, live, H120 strain", (1000, 2500, 5000), "0.011", 1),
        ("IBD", "Infectious bursal disease vaccine, live, intermediate", (1000, 2500), "0.014", 1),
        ("MDV", "Marek's disease vaccine, HVT, freeze-dried", (1000, 2000), "0.016", 1),
        ("FPX", "Fowl pox vaccine, live, wing web", (500, 1000), "0.018", 1),
        ("AIV", "Avian influenza H5 vaccine, inactivated", (500, 1000), "0.045", 0),
        ("FCH", "Fowl cholera vaccine, inactivated", (500, 1000), "0.03", 0),
        ("ILT", "Infectious laryngotracheitis vaccine, live", (1000, 2500), "0.02", 1),
        ("EDS", "Egg drop syndrome vaccine, inactivated", (500, 1000), "0.032", 0),
        ("FMO", "Foot-and-mouth disease vaccine, serotype O, inactivated", (25, 50, 100), "0.9", 0),
        ("FMA", "Foot-and-mouth disease vaccine, serotype A, inactivated", (25, 50), "0.95", 0),
        ("FAS", "Foot-and-mouth disease vaccine, Asia 1, inactivated", (25, 50), "0.95", 0),
        ("PPR", "Peste des petits ruminants vaccine, live", (100, 500), "0.09", 1),
        ("LSD", "Lumpy skin disease vaccine, live, Neethling strain", (50, 100), "0.35", 1),
        ("SPX", "Sheep pox vaccine, live", (100, 500), "0.08", 1),
        ("GPX", "Goat pox vaccine, live", (100, 500), "0.08", 1),
        ("ANT", "Anthrax spore vaccine, live, Sterne strain", (50, 100), "0.12", 1),
        ("BQL", "Blackleg vaccine, inactivated", (50, 100), "0.14", 0),
        ("HSV", "Haemorrhagic septicaemia vaccine, oil adjuvant", (50, 100), "0.16", 0),
        ("S19", "Brucella abortus S19 vaccine, live", (10, 25), "0.6", 1),
        ("RVF", "Rift Valley fever vaccine, inactivated", (50, 100), "0.55", 0),
        ("ENT", "Clostridial enterotoxaemia vaccine, inactivated", (50, 100), "0.11", 0),
        ("BTV", "Bluetongue vaccine, serotype 8, inactivated", (50, 100), "0.48", 0),
        ("CSF", "Classical swine fever vaccine, live, C strain", (10, 50), "0.4", 1),
        ("PRS", "PRRS vaccine, live", (10, 50), "0.85", 1),
        ("PCV", "Porcine circovirus type 2 vaccine, inactivated", (10, 50), "0.7", 0),
        ("MHY", "Mycoplasma hyopneumoniae vaccine, inactivated", (10, 50), "0.55", 0),
        ("ERY", "Swine erysipelas vaccine, inactivated", (50, 100), "0.3", 0),
        ("RAB", "Rabies vaccine for dogs, inactivated", (1, 10), "1.6", 0),
        ("CDV", "Canine distemper vaccine, live", (1, 10), "2.1", 1),
        ("DPV", "Duck plague vaccine, live", (500, 1000), "0.02", 1),
        ("COR", "Infectious coryza vaccine, inactivated", (500, 1000), "0.04", 0),
        ("ORF", "Orf vaccine, live", (100,), "0.1", 1),
        ("CCP", "Contagious caprine pleuropneumonia vaccine, inactivated", (50, 100), "0.2", 0),
        ("LEP", "Leptospirosis vaccine for cattle, inactivated", (50,), "0.28", 0),
    )
    return [
        Product(
            code=f"VAC-{code}-{doses}",
            name=f"{name}, {doses:,} dose{'s' if doses > 1 else ''}",
            family=Family.VACCINE,
            unit="XVI",
            cost=max(Decimal(per_dose) * doses, Decimal("3.20")).quantize(Decimal("0.01")),
            weight=Decimal("0.05") if doses < 1000 else Decimal("0.08"),
            storage=Storage.CHILLED,
            shelf_life=540 if live else 730,
            dangerous=None,
            commodity="3002 42 00",
        )
        for code, name, sizes, per_dose, live in table
        for doses in sizes
    ]


def _kits() -> list[Product]:
    elisa = (
        ("AIAB", "Avian influenza antibody", (192, 960)),
        ("H5AB", "Avian influenza H5 antibody", (192, 480)),
        ("NDAB", "Newcastle disease antibody", (192, 480, 960)),
        ("IBDA", "Infectious bursal disease antibody", (192, 480, 960)),
        ("IBVA", "Infectious bronchitis antibody", (192, 480, 960)),
        ("MGAB", "Mycoplasma gallisepticum antibody", (192, 480, 960)),
        ("MSAB", "Mycoplasma synoviae antibody", (192, 480)),
        ("FNSP", "Foot-and-mouth disease NSP antibody", (192, 480, 960)),
        ("FMDO", "Foot-and-mouth disease type O antibody", (192, 480, 960)),
        ("PPRC", "Peste des petits ruminants competitive", (192, 480, 960)),
        ("BRUI", "Brucella antibody, indirect, multi-species", (192, 480, 960)),
        ("BRUC", "Brucella antibody, competitive", (192, 480, 960)),
        ("BTBG", "Bovine tuberculosis interferon-gamma", (192, 480)),
        ("BVDG", "Bovine viral diarrhoea antigen", (192, 480)),
        ("BVDA", "Bovine viral diarrhoea antibody", (192, 480, 960)),
        ("IBRG", "IBR gB antibody", (192, 480, 960)),
        ("LEPA", "Leptospira antibody", (192,)),
        ("QFEV", "Q fever antibody", (192, 480, 960)),
        ("ASFA", "African swine fever antibody", (192, 480, 960)),
        ("CSFA", "Classical swine fever antibody", (192, 480, 960)),
        ("PRRA", "PRRS antibody", (192, 480, 960)),
        ("PCVA", "Porcine circovirus 2 antibody", (192,)),
        ("TOXO", "Toxoplasma antibody, multi-species", (192,)),
        ("NEOS", "Neospora caninum antibody", (192,)),
        ("BTVA", "Bluetongue antibody, competitive", (192, 480, 960)),
        ("CPXA", "Capripox antibody", (192, 480)),
        ("RVFA", "Rift Valley fever antibody", (192,)),
        ("PTBA", "Paratuberculosis antibody", (192, 480, 960)),
        ("FASC", "Fasciola hepatica antibody", (192,)),
        ("SALA", "Salmonella antibody, poultry", (192, 480)),
    )
    pcr = (
        ("ASF", "African swine fever", (48, 384)),
        ("CSF", "Classical swine fever", (48, 96, 384)),
        ("FMD", "Foot-and-mouth disease, pan-serotype", (48, 96)),
        ("AIM", "Avian influenza matrix gene", (48, 96, 384)),
        ("AH5", "Avian influenza H5 subtype", (48, 96)),
        ("AH7", "Avian influenza H7 subtype", (48, 96)),
        ("AH9", "Avian influenza H9 subtype", (48, 96)),
        ("NDV", "Newcastle disease virus", (48, 96, 384)),
        ("PPR", "Peste des petits ruminants virus", (48, 96, 384)),
        ("CPX", "Capripox virus, lumpy skin disease and pox", (48, 96)),
        ("RVF", "Rift Valley fever virus", (48, 96)),
        ("BRU", "Brucella species", (48, 96)),
        ("MTB", "Mycobacterium tuberculosis complex", (48, 96)),
        ("SAL", "Salmonella species", (48, 96)),
        ("LEP", "Pathogenic Leptospira", (48, 96)),
        ("PRR", "PRRS virus, types 1 and 2", (48, 96)),
        ("PCV", "Porcine circovirus 2", (48, 96)),
        ("IBV", "Infectious bronchitis virus", (48, 96)),
        ("MGS", "Mycoplasma gallisepticum and synoviae", (48, 96)),
        ("COX", "Coxiella burnetii", (48, 96)),
    )
    rapid = (
        ("AIAG", "Avian influenza antigen", (10, 25)),
        ("NDAG", "Newcastle disease antigen", (10, 25)),
        ("IBDG", "Infectious bursal disease antigen", (10, 25)),
        ("FMDG", "Foot-and-mouth disease antigen", (10, 25)),
        ("PPRG", "Peste des petits ruminants antigen", (10, 25)),
        ("RABG", "Rabies antigen", (10, 25)),
        ("CPVG", "Canine parvovirus antigen", (10, 25)),
        ("CSFG", "Classical swine fever antigen", (10, 25)),
        ("ASFG", "African swine fever antigen", (10, 25)),
        ("BRUR", "Brucella Rose Bengal antigen", (10, 25)),
        ("TOXG", "Toxoplasma antibody", (10, 25)),
    )
    return [
        *(
            Product(
                code=f"ELI-{code}-{tests}",
                name=f"{target} ELISA kit, {tests:,} tests",
                family=Family.ELISA,
                unit="KT",
                cost=(Decimal("1.05") * tests + 90).quantize(Decimal(1)),
                weight=Decimal("0.6") + Decimal(tests) / 480,
                storage=Storage.CHILLED,
                shelf_life=450,
                dangerous=None,
                commodity="3822 19 00",
            )
            for code, target, sizes in elisa
            for tests in sizes
        ),
        *(
            Product(
                code=f"PCR-{code}-{reactions}",
                name=f"{target} real-time PCR kit, {reactions} reactions",
                family=Family.PCR,
                unit="KT",
                cost=(Decimal("4.6") * reactions + 60).quantize(Decimal(1)),
                weight=Decimal("0.3") + Decimal(reactions) / 960,
                storage=Storage.FROZEN,
                shelf_life=365,
                dangerous=None,
                commodity="3822 19 00",
            )
            for code, target, sizes in pcr
            for reactions in sizes
        ),
        *(
            Product(
                code=f"RDT-{code}-{tests}",
                name=f"{target} rapid test, {tests} tests",
                family=Family.RAPID,
                unit="KT",
                cost=(Decimal("3.4") * tests + 12).quantize(Decimal(1)),
                weight=Decimal("0.25") + Decimal(tests) / 100,
                storage=Storage.AMBIENT,
                shelf_life=540,
                dangerous=None,
                commodity="3822 19 00",
            )
            for code, target, sizes in rapid
            for tests in sizes
        ),
    ]


def _supplies() -> list[Product]:
    chilled, frozen, ambient = Storage.CHILLED, Storage.FROZEN, Storage.AMBIENT
    reagents = (
        # Code, name, unit, pounds, kilograms, storage, shelf life, UN number, commodity.
        (
            "EXT-50",
            "Nucleic acid extraction kit, spin column, 50 preps",
            "KT",
            "118",
            "0.6",
            ambient,
            540,
            None,
            "3822 19 00",
        ),
        (
            "EXT-250",
            "Nucleic acid extraction kit, spin column, 250 preps",
            "KT",
            "455",
            "1.9",
            ambient,
            540,
            None,
            "3822 19 00",
        ),
        (
            "MAG-96",
            "Magnetic bead extraction kit, 96 preps",
            "KT",
            "210",
            "0.9",
            ambient,
            365,
            None,
            "3822 19 00",
        ),
        (
            "MAG-960",
            "Magnetic bead extraction kit, 960 preps",
            "KT",
            "1490",
            "5.2",
            ambient,
            365,
            None,
            "3822 19 00",
        ),
        (
            "RTMM-500",
            "One-step RT-qPCR master mix, 500 reactions",
            "KT",
            "265",
            "0.4",
            frozen,
            365,
            None,
            "3822 19 00",
        ),
        (
            "RTMM-2000",
            "One-step RT-qPCR master mix, 2,000 reactions",
            "KT",
            "890",
            "0.9",
            frozen,
            365,
            None,
            "3822 19 00",
        ),
        (
            "QMM-500",
            "qPCR probe master mix, 500 reactions",
            "KT",
            "185",
            "0.4",
            frozen,
            365,
            None,
            "3822 19 00",
        ),
        (
            "QMM-2000",
            "qPCR probe master mix, 2,000 reactions",
            "KT",
            "620",
            "0.9",
            frozen,
            365,
            None,
            "3822 19 00",
        ),
        ("PRK-100", "Proteinase K, 100 mg", "XTU", "64", "0.2", frozen, 730, None, "3507 90 90"),
        (
            "LAD-100",
            "DNA ladder, 100 bp, 500 µl",
            "XTU",
            "58",
            "0.2",
            frozen,
            730,
            None,
            "3822 90 00",
        ),
        (
            "NFW-500",
            "Nuclease-free water, 500 ml",
            "XBO",
            "24",
            "0.7",
            ambient,
            1095,
            None,
            "2853 90 10",
        ),
        (
            "ETH-2L5",
            "Ethanol absolute, molecular grade, 2.5 l",
            "XBO",
            "38",
            "2.6",
            ambient,
            1095,
            "UN1170, class 3",
            "2207 10 00",
        ),
        (
            "IPA-2L5",
            "Isopropanol, molecular grade, 2.5 l",
            "XBO",
            "34",
            "2.5",
            ambient,
            1095,
            "UN1219, class 3",
            "2905 12 00",
        ),
        (
            "MEO-2L5",
            "Methanol, analytical grade, 2.5 l",
            "XBO",
            "29",
            "2.5",
            ambient,
            1095,
            "UN1230, class 3",
            "2905 11 00",
        ),
        (
            "ACE-2L5",
            "Acetone, analytical grade, 2.5 l",
            "XBO",
            "27",
            "2.4",
            ambient,
            1095,
            "UN1090, class 3",
            "2914 11 00",
        ),
        (
            "XYL-2L5",
            "Xylene, histology grade, 2.5 l",
            "XBO",
            "41",
            "2.7",
            ambient,
            1095,
            "UN1307, class 3",
            "2902 44 00",
        ),
        (
            "NBF-5L",
            "Formalin, 10% neutral buffered, 5 l",
            "XBO",
            "36",
            "5.6",
            ambient,
            730,
            "UN3334, class 9",
            "2912 11 00",
        ),
        (
            "HYP-5L",
            "Sodium hypochlorite solution, 12%, 5 l",
            "XBO",
            "19",
            "6.1",
            ambient,
            365,
            "UN1791, class 8",
            "2828 90 00",
        ),
        (
            "HCL-2L5",
            "Hydrochloric acid, 37%, 2.5 l",
            "XBO",
            "33",
            "3.2",
            ambient,
            1095,
            "UN1789, class 8",
            "2806 10 00",
        ),
        (
            "NAOH-1K",
            "Sodium hydroxide pellets, 1 kg",
            "XJR",
            "22",
            "1.2",
            ambient,
            1095,
            "UN1823, class 8",
            "2815 11 00",
        ),
        (
            "STOP-500",
            "ELISA stop solution, 0.5 M sulfuric acid, 500 ml",
            "XBO",
            "21",
            "0.8",
            ambient,
            730,
            "UN2796, class 8",
            "2807 00 00",
        ),
        ("TMB-100", "TMB substrate, 100 ml", "XBO", "46", "0.3", chilled, 365, None, "3822 90 00"),
        (
            "WASH-1L",
            "ELISA wash buffer concentrate, 20x, 1 l",
            "XBO",
            "28",
            "1.2",
            ambient,
            730,
            None,
            "3822 90 00",
        ),
        (
            "PBS-100",
            "Phosphate buffered saline tablets, 100",
            "XBO",
            "26",
            "0.3",
            ambient,
            1095,
            None,
            "3822 90 00",
        ),
        (
            "BSA-100",
            "Bovine serum albumin, fraction V, 100 g",
            "XBO",
            "92",
            "0.3",
            chilled,
            730,
            None,
            "3502 90 90",
        ),
        (
            "AGA-500",
            "Agarose, molecular grade, 500 g",
            "XBO",
            "145",
            "0.8",
            ambient,
            1095,
            None,
            "3913 90 00",
        ),
        ("GLY-1L", "Glycerol, 1 l", "XBO", "18", "1.4", ambient, 1095, None, "2905 45 00"),
        (
            "VTM-50",
            "Viral transport medium, 3 ml tubes, pack of 50",
            "XPK",
            "48",
            "0.9",
            chilled,
            365,
            None,
            "3821 00 00",
        ),
        (
            "BHI-500",
            "Brain heart infusion broth, 500 g",
            "XJR",
            "62",
            "0.7",
            ambient,
            1095,
            None,
            "3821 00 00",
        ),
        (
            "BAB-500",
            "Blood agar base, 500 g",
            "XJR",
            "58",
            "0.7",
            ambient,
            1095,
            None,
            "3821 00 00",
        ),
        ("MAC-500", "MacConkey agar, 500 g", "XJR", "54", "0.7", ambient, 1095, None, "3821 00 00"),
        (
            "TBB-50",
            "Bovine tuberculin PPD, 50 doses",
            "XVI",
            "42",
            "0.1",
            chilled,
            730,
            None,
            "3002 49 00",
        ),
        (
            "TBA-50",
            "Avian tuberculin PPD, 50 doses",
            "XVI",
            "42",
            "0.1",
            chilled,
            730,
            None,
            "3002 49 00",
        ),
        (
            "GIE-500",
            "Giemsa stain solution, 500 ml",
            "XBO",
            "31",
            "0.8",
            ambient,
            1095,
            "UN1230, class 3",
            "3204 90 00",
        ),
        (
            "GRAM-KT",
            "Gram stain kit, 4 x 250 ml",
            "KT",
            "39",
            "1.3",
            ambient,
            1095,
            "UN1170, class 3",
            "3822 90 00",
        ),
        ("OIL-100", "Immersion oil, 100 ml", "XBO", "14", "0.2", ambient, 1825, None, "2710 19 81"),
        (
            "DIL-200",
            "Vaccine diluent, 200 ml",
            "XBO",
            "6",
            "0.3",
            ambient,
            1095,
            None,
            "3004 90 00",
        ),
        (
            "DIL-1000",
            "Vaccine diluent, 1,000 ml",
            "XBO",
            "11",
            "1.2",
            ambient,
            1095,
            None,
            "3004 90 00",
        ),
        (
            "DYE-100",
            "Blue vaccination dye tablets, 100",
            "XBO",
            "17",
            "0.2",
            ambient,
            1095,
            None,
            "3204 90 00",
        ),
        (
            "NACL-1K",
            "Sodium chloride, analytical grade, 1 kg",
            "XJR",
            "16",
            "1.1",
            ambient,
            1825,
            None,
            "2501 00 91",
        ),
        (
            "TRIS-500",
            "Tris base, molecular grade, 500 g",
            "XJR",
            "58",
            "0.6",
            ambient,
            1825,
            None,
            "2922 19 00",
        ),
        (
            "QCS-SAL",
            "Salmonella Typhimurium quality control strain, freeze-dried, pack of 2",
            "XPK",
            "96",
            "0.2",
            chilled,
            730,
            "UN3373",
            "3002 49 00",
        ),
        (
            "QCS-STA",
            "Staphylococcus aureus quality control strain, freeze-dried, pack of 2",
            "XPK",
            "92",
            "0.2",
            chilled,
            730,
            "UN3373",
            "3002 49 00",
        ),
    )
    plastics = (
        # Code, name, unit, pounds, kilograms, commodity.
        ("TIPF-10", "Filter pipette tips, 10 µl, 10 racks of 96", "XBX", "46", "0.9", "3926 90 97"),
        ("TIPF-20", "Filter pipette tips, 20 µl, 10 racks of 96", "XBX", "46", "0.9", "3926 90 97"),
        (
            "TIPF-200",
            "Filter pipette tips, 200 µl, 10 racks of 96",
            "XBX",
            "49",
            "1.0",
            "3926 90 97",
        ),
        (
            "TIPF-1000",
            "Filter pipette tips, 1,000 µl, 10 racks of 96",
            "XBX",
            "54",
            "1.3",
            "3926 90 97",
        ),
        ("TIPB-200", "Pipette tips, 200 µl, bag of 1,000", "XBG", "12", "0.6", "3926 90 97"),
        ("TIPB-1000", "Pipette tips, 1,000 µl, bag of 1,000", "XBG", "15", "0.9", "3926 90 97"),
        ("PLT-96", "PCR plates, 96 wells, pack of 25", "XPK", "62", "1.1", "3926 90 97"),
        ("PLT-384", "PCR plates, 384 wells, pack of 25", "XPK", "88", "1.0", "3926 90 97"),
        ("STR-8", "PCR tube strips, 8 x 0.2 ml, pack of 125", "XPK", "39", "0.5", "3926 90 97"),
        ("SEAL-100", "Optical adhesive seals, pack of 100", "XPK", "71", "0.6", "3919 90 80"),
        ("MCT-15", "Microcentrifuge tubes, 1.5 ml, bag of 500", "XBG", "14", "0.6", "3926 90 97"),
        ("MCT-20", "Microcentrifuge tubes, 2.0 ml, bag of 500", "XBG", "15", "0.7", "3926 90 97"),
        ("CRY-2", "Cryovials, 2 ml, pack of 500", "XPK", "68", "1.4", "3926 90 97"),
        ("CRY-5", "Cryovials, 5 ml, pack of 500", "XPK", "84", "2.2", "3926 90 97"),
        (
            "ELP-96",
            "ELISA microplates, 96 wells, high binding, pack of 50",
            "XPK",
            "96",
            "2.1",
            "3926 90 97",
        ),
        ("SER-5", "Serological pipettes, 5 ml, case of 200", "XCS", "34", "2.4", "3926 90 97"),
        ("SER-10", "Serological pipettes, 10 ml, case of 200", "XCS", "36", "3.1", "3926 90 97"),
        ("SER-25", "Serological pipettes, 25 ml, case of 200", "XCS", "52", "4.6", "3926 90 97"),
        ("CEN-15", "Centrifuge tubes, 15 ml, case of 500", "XCS", "58", "4.1", "3926 90 97"),
        ("CEN-50", "Centrifuge tubes, 50 ml, case of 500", "XCS", "74", "7.2", "3926 90 97"),
        (
            "BCT-PLN",
            "Blood collection tubes, plain, 4 ml, box of 100",
            "XBX",
            "16",
            "0.8",
            "9018 39 00",
        ),
        (
            "BCT-EDTA",
            "Blood collection tubes, EDTA, 4 ml, box of 100",
            "XBX",
            "18",
            "0.8",
            "9018 39 00",
        ),
        (
            "BCT-HEP",
            "Blood collection tubes, heparin, 4 ml, box of 100",
            "XBX",
            "21",
            "0.8",
            "9018 39 00",
        ),
        ("VND-21", "Blood collection needles, 21G, box of 100", "XBX", "13", "0.4", "9018 32 10"),
        ("SYR-2", "Syringes, 2 ml, box of 100", "XBX", "8", "0.6", "9018 31 10"),
        ("SYR-5", "Syringes, 5 ml, box of 100", "XBX", "9", "0.9", "9018 31 10"),
        ("SYR-10", "Syringes, 10 ml, box of 100", "XBX", "11", "1.3", "9018 31 10"),
        ("NDL-18", "Hypodermic needles, 18G, box of 100", "XBX", "6", "0.3", "9018 32 10"),
        ("NDL-21", "Hypodermic needles, 21G, box of 100", "XBX", "6", "0.3", "9018 32 10"),
        ("SWB-100", "Flocked swabs, sterile, pack of 100", "XPK", "29", "0.5", "3005 90 99"),
        ("BAG-2L", "Sample bags, 2 l, box of 500", "XBX", "45", "3.2", "3923 21 00"),
        ("GLV-S", "Nitrile gloves, small, box of 100", "XBX", "5", "0.6", "4015 19 00"),
        ("GLV-M", "Nitrile gloves, medium, box of 100", "XBX", "5", "0.6", "4015 19 00"),
        ("GLV-L", "Nitrile gloves, large, box of 100", "XBX", "5", "0.7", "4015 19 00"),
        ("GLV-XL", "Nitrile gloves, extra large, box of 100", "XBX", "5", "0.7", "4015 19 00"),
        ("FFP3-20", "FFP3 respirators, box of 20", "XBX", "38", "0.8", "6307 90 98"),
        ("COAT-10", "Disposable laboratory coats, pack of 10", "XPK", "24", "1.6", "6210 10 98"),
        ("BIO-200", "Autoclavable biohazard bags, box of 200", "XBX", "32", "4.0", "3923 21 00"),
        ("SHP-5", "Sharps containers, 5 l, case of 20", "XCS", "48", "6.5", "3923 90 00"),
        ("PET-90", "Petri dishes, 90 mm, case of 500", "XCS", "39", "5.8", "3926 90 97"),
        ("LOOP-1K", "Inoculation loops, 10 µl, pack of 1,000", "XPK", "27", "0.9", "3926 90 97"),
        ("SLD-72", "Microscope slides, frosted end, box of 72", "XBX", "7", "0.6", "7017 90 00"),
        ("COV-22", "Cover slips, 22 mm, box of 1,000", "XBX", "12", "0.5", "7017 90 00"),
        ("FILM-38", "Laboratory sealing film, 100 mm x 38 m", "EA", "29", "0.3", "3920 10 89"),
        ("TAG-100", "Livestock ear tags, numbered, pack of 100", "XPK", "36", "0.8", "3926 90 97"),
        ("TAGP-1", "Ear tag applicator", "EA", "24", "0.4", "8205 59 80"),
        ("GUN-5", "Automatic dosing syringe, 5 ml", "EA", "42", "0.3", "9018 31 90"),
        ("GUN-50", "Automatic drenching gun, 50 ml", "EA", "58", "0.5", "8424 20 00"),
        ("VSY-1", "Continuous vaccinator, 1 ml", "EA", "36", "0.3", "9018 31 90"),
        ("WWA-100", "Wing web applicators, pack of 100", "XPK", "21", "0.2", "9018 32 90"),
        ("DRP-100", "Vaccine eye droppers, pack of 100", "XPK", "14", "0.3", "3926 90 97"),
        (
            "BEAD-50",
            "Bead tubes for homogenising, 2 ml, pack of 50",
            "XPK",
            "52",
            "0.4",
            "3926 90 97",
        ),
        (
            "CRB-81",
            "Cryogenic storage boxes, 81 places, pack of 5",
            "XPK",
            "31",
            "0.9",
            "3926 90 97",
        ),
        ("RCK-80", "Tube racks, 1.5 ml, 80 places, pack of 5", "XPK", "22", "0.8", "3926 90 97"),
        ("BIN-30", "Pedal waste bin, 30 l", "EA", "48", "4.2", "3924 90 00"),
        ("LENS-100", "Lens cleaning tissue, booklets of 100", "XPK", "9", "0.1", "4803 00 31"),
        ("PEN-10", "Laboratory marker pens, pack of 10", "XPK", "11", "0.2", "9608 20 00"),
        ("LBL-1K", "Freezer labels, roll of 1,000", "EA", "26", "0.4", "4821 10 10"),
        ("WGB-500", "Weighing boats, pack of 500", "XPK", "18", "0.7", "3926 90 97"),
    )
    made = [
        Product(f"RGT-{c}", n, Family.REAGENT, u, Decimal(p), Decimal(w), s, life, un, hs)
        for c, n, u, p, w, s, life, un, hs in reagents
    ]
    made += [
        Product(
            f"CON-{c}", n, Family.CONSUMABLE, u, Decimal(p), Decimal(w), ambient, None, None, hs
        )
        for c, n, u, p, w, hs in plastics
    ]
    return made


def _equipment() -> list[Product]:
    instruments = (
        # Code, name, pounds, kilograms packed, commodity.
        ("QPCR48", "Real-time PCR thermocycler, 48 wells", "14200", "38", "9027 89 00"),
        ("QPCR384", "Real-time PCR thermocycler, 384 wells", "31500", "72", "9027 89 00"),
        ("PCR96", "Gradient PCR thermocycler, 96 wells", "5400", "24", "8419 89 98"),
        ("QPCRF", "Portable real-time PCR instrument, field, 16 wells", "9800", "12", "9027 89 00"),
        ("READ", "Microplate reader, absorbance, 8 filters", "6900", "26", "9027 50 00"),
        ("WASH", "Microplate washer, 8 channels", "5100", "28", "8422 19 00"),
        ("MCF24", "Microcentrifuge, 24 x 1.5 ml", "1450", "16", "8421 19 70"),
        ("CFSW", "Benchtop centrifuge, swing-out rotor, 4 x 250 ml", "6200", "62", "8421 19 70"),
        ("BSC15", "Biosafety cabinet, class II, 1.5 m", "8900", "285", "8414 60 00"),
        ("BSC18", "Biosafety cabinet, class II, 1.8 m", "10400", "320", "8414 60 00"),
        ("PCRWS", "PCR workstation, UV, 0.8 m", "3600", "68", "8414 60 00"),
        ("CO2I", "CO2 incubator, 170 l", "7400", "125", "8419 89 98"),
        ("INC100", "Laboratory incubator, 100 l", "2300", "64", "8419 89 98"),
        ("FR400", "Laboratory refrigerator, 2 to 8 °C, 400 l", "2900", "118", "8418 50 19"),
        ("FZ300", "Laboratory freezer, -20 °C, 300 l", "3400", "112", "8418 40 20"),
        ("ULT500", "Ultra-low temperature freezer, -80 °C, 500 l", "11800", "265", "8418 40 20"),
        ("AC23", "Benchtop autoclave, 23 l", "4100", "52", "8419 20 00"),
        ("AC75", "Vertical autoclave, 75 l", "8700", "145", "8419 20 00"),
        ("H2O1", "Water purification system, type I", "6300", "45", "8421 21 00"),
        ("PIP10", "Single-channel pipette, 0.5 to 10 µl", "245", "0.4", "8413 20 00"),
        ("PIP20", "Single-channel pipette, 2 to 20 µl", "245", "0.4", "8413 20 00"),
        ("PIP200", "Single-channel pipette, 20 to 200 µl", "245", "0.4", "8413 20 00"),
        ("PIP1000", "Single-channel pipette, 100 to 1,000 µl", "245", "0.4", "8413 20 00"),
        ("MPIP8", "Multichannel pipette, 8 channels, 30 to 300 µl", "720", "0.7", "8413 20 00"),
        ("MPIP12", "Multichannel pipette, 12 channels, 30 to 300 µl", "820", "0.8", "8413 20 00"),
        ("PIPC", "Pipette controller", "310", "0.9", "8413 20 00"),
        ("BAL220", "Analytical balance, 220 g x 0.1 mg", "2600", "11", "8423 81 10"),
        ("BAL2K", "Precision balance, 2 kg x 0.01 g", "980", "7", "8423 81 10"),
        ("PHM", "Benchtop pH meter", "720", "4", "9027 89 00"),
        ("VTX", "Vortex mixer", "260", "4", "8479 82 00"),
        ("STIR", "Magnetic stirrer hotplate", "540", "5", "8516 79 70"),
        ("SHAKE", "Orbital shaker, platform 45 x 45 cm", "1650", "22", "8479 82 00"),
        ("ROCK", "Rocking platform", "690", "8", "8479 82 00"),
        ("WB12", "Water bath, 12 l", "780", "11", "8419 89 98"),
        ("DBH", "Dry block heater, two blocks", "860", "6", "8419 89 98"),
        ("MICL", "Compound microscope, binocular, LED", "1350", "11", "9011 80 00"),
        ("MICF", "Fluorescence microscope, LED, trinocular", "8400", "24", "9011 80 00"),
        ("MICS", "Stereo microscope, zoom", "1650", "9", "9011 20 90"),
        (
            "GEL",
            "Horizontal gel electrophoresis system with power supply",
            "1480",
            "14",
            "8543 70 90",
        ),
        ("GDOC", "Gel documentation system, blue light", "4900", "22", "9027 50 00"),
        ("XTR32", "Automated nucleic acid extractor, 32 samples", "19800", "68", "8479 89 97"),
        ("SPEC", "Micro-volume spectrophotometer", "7600", "9", "9027 30 00"),
        ("OVEN", "Hot air oven, 100 l", "2100", "68", "8419 89 98"),
        ("ICE", "Flake ice maker, 85 kg a day", "3100", "72", "8418 69 00"),
        ("LN35", "Liquid nitrogen dewar, 35 l", "1250", "21", "7311 00 99"),
        ("HAEM", "Veterinary haematology analyser, 3-part", "9400", "28", "9027 89 00"),
        ("BIOC", "Veterinary biochemistry analyser", "11600", "34", "9027 89 00"),
        ("NECK", "Field necropsy kit, stainless steel", "640", "8", "9018 90 84"),
        ("UPS3", "Uninterruptible power supply, 3 kVA", "1450", "36", "8504 40 30"),
        ("GEN5", "Portable generator, 5 kVA", "1850", "92", "8502 11 20"),
        ("MINI", "Mini centrifuge for strips and tubes", "210", "2", "8421 19 70"),
        ("ASP", "Vacuum aspiration system", "890", "9", "8414 10 89"),
        ("TIMR", "Laboratory timer, four channels", "28", "0.2", "9106 90 00"),
        ("BARC", "Barcode reader for sample tubes", "240", "0.8", "8471 90 00"),
        ("FUME", "Ductless fume cupboard, 1.0 m", "6400", "145", "8414 60 00"),
        ("SLDW", "Slide warmer", "380", "4", "8419 89 98"),
        ("REPP", "Electronic repeating pipette", "640", "0.6", "8413 20 00"),
        ("HOMO", "Bead mill homogeniser, 24 tubes", "4600", "18", "8479 82 00"),
    )
    cold_chain = (
        ("VC17", "Vaccine carrier, 1.7 l", "38", "2.4", None, "3923 10 90"),
        ("VC26", "Vaccine carrier, 2.6 l", "46", "2.9", None, "3923 10 90"),
        ("CB20", "Long-range cold box, 20 l, 96 hours", "265", "14", None, "3923 10 90"),
        ("CB25", "Long-range cold box, 25 l, 120 hours", "320", "17", None, "3923 10 90"),
        ("ICE04", "Ice packs, 0.4 l, pack of 24", "42", "10", None, "3926 90 97"),
        ("ICE06", "Ice packs, 0.6 l, pack of 24", "48", "15", None, "3926 90 97"),
        ("PCM5", "Phase change packs, +5 °C, pack of 12", "96", "9", None, "3824 99 96"),
        (
            "LOGU",
            "Temperature data logger, single use, pack of 10",
            "120",
            "0.6",
            730,
            "9025 19 00",
        ),
        ("LOGM", "Temperature data logger, multi-use, USB", "68", "0.2", None, "9025 19 00"),
        ("REC30", "30-day electronic temperature recorder", "54", "0.2", 1095, "9025 19 00"),
        ("FRZI", "Freeze indicators, pack of 100", "145", "0.4", 1095, "9025 19 00"),
        ("SH24", "Insulated shipper, 2 to 8 °C, 24 l, 72 hours", "58", "4.8", None, "3923 10 90"),
        ("SH48", "Insulated shipper, 2 to 8 °C, 48 l, 96 hours", "94", "7.6", None, "3923 10 90"),
        ("SHDI", "Dry ice shipper, 30 l", "72", "5.2", None, "3923 10 90"),
        ("ILR140", "Ice-lined refrigerator, 140 l", "2450", "112", None, "8418 50 19"),
        (
            "SDD50",
            "Solar direct-drive vaccine refrigerator, 50 l",
            "3900",
            "96",
            None,
            "8418 50 19",
        ),
        (
            "SDD100",
            "Solar direct-drive vaccine refrigerator, 100 l",
            "5200",
            "138",
            None,
            "8418 50 19",
        ),
        ("RTM", "Remote temperature monitoring device, cellular", "185", "0.5", None, "9025 19 00"),
        ("THMM", "Digital minimum and maximum thermometer", "16", "0.1", None, "9025 19 00"),
        ("CBAG", "Sample transport cool bag, 10 l", "34", "1.1", None, "4202 92 19"),
        (
            "UN3373",
            "Biological substance transport packaging, category B, pack of 10",
            "88",
            "3.4",
            None,
            "4819 10 00",
        ),
        ("CRALM", "Cold room temperature alarm", "245", "1.2", None, "8531 10 30"),
        (
            "CB72L",
            "Sample transport cool box, 12 l, validated 48 hours",
            "128",
            "5.4",
            None,
            "3923 10 90",
        ),
    )
    made = [
        Product(
            f"EQP-{c}",
            n,
            Family.EQUIPMENT,
            "EA",
            Decimal(p),
            Decimal(w),
            Storage.AMBIENT,
            None,
            None,
            hs,
        )
        for c, n, p, w, hs in instruments
    ]
    made += [
        Product(
            f"CCH-{c}",
            n,
            Family.COLD_CHAIN,
            "XPK" if "pack of" in n else "EA",
            Decimal(p),
            Decimal(w),
            Storage.AMBIENT,
            life,
            None,
            hs,
        )
        for c, n, p, w, life, hs in cold_chain
    ]
    return made


PRODUCTS: tuple[Product, ...] = (*_vaccines(), *_kits(), *_supplies(), *_equipment())
"""Everything history sells, beyond the nine items the month's setup adds."""


def _maker(
    code: str,
    name: str,
    country: str,
    contact: tuple[str, str],
    town: str,
    currency: str,
    families: tuple[Family, ...],
    lead_time: tuple[int, int],
    share: int = 1,
    phone: str | None = None,
) -> Maker:
    domain = code.removeprefix("S-").lower()
    person, role = contact
    given, family = person.lower().split()[0], person.lower().split()[-1]
    return Maker(
        Organisation(
            code,
            name,
            country,
            (Contact(person, role, f"{given}.{family}@{domain}.example", phone),),
            (("Head office", (name, town)),),
        ),
        currency,
        families,
        lead_time,
        share,
    )


def _known(code: str) -> Organisation:
    return next(organisation for organisation in PARTIES if organisation.code == code)


V, E, P, R = Family.VACCINE, Family.ELISA, Family.PCR, Family.RAPID
G, C, Q, K = Family.REAGENT, Family.CONSUMABLE, Family.EQUIPMENT, Family.COLD_CHAIN

MAKERS: tuple[Maker, ...] = (
    Maker(_known("S-NORDVET"), "DKK", (V,), (21, 42), 4),
    _maker(
        "S-HELVORA",
        "Helvora Animal Vaccines B.V.",
        "NL",
        ("Sanne de Wit", "Export sales"),
        "Lelystad",
        "EUR",
        (V,),
        (28, 49),
        2,
    ),
    _maker(
        "S-BIOSAVANE",
        "Biosavane SAS",
        "FR",
        ("Camille Roussel", "Export orders"),
        "Lyon",
        "EUR",
        (V,),
        (21, 35),
    ),
    _maker(
        "S-LUSITAVET",
        "Lusitavet Biológicos, Lda",
        "PT",
        ("Tiago Marques", "Commercial director"),
        "Porto",
        "EUR",
        (V,),
        (28, 56),
    ),
    _maker(
        "S-CORVALE",
        "Corvale Biologics Inc.",
        "US",
        ("Dana Whitcomb", "International sales"),
        "Omaha, Nebraska",
        "USD",
        (V,),
        (35, 63),
        2,
    ),
    _maker(
        "S-TAMBERG",
        "Tamberg Impfstoffwerk GmbH",
        "AT",
        ("Lukas Brandner", "Order desk"),
        "Linz",
        "EUR",
        (V,),
        (21, 42),
    ),
    Maker(_known("S-AURELIAN"), "EUR", (E, P, R), (10, 21), 4),
    _maker(
        "S-QUILLAN",
        "Quillan Diagnostics Ltd",
        "IE",
        ("Aoife Brennan", "Customer services"),
        "Galway",
        "EUR",
        (E, R),
        (14, 28),
        2,
    ),
    _maker(
        "S-MERIDOX",
        "Meridox Bioscience Inc.",
        "US",
        ("Marcus Hale", "Distributor sales"),
        "Madison, Wisconsin",
        "USD",
        (E, P),
        (14, 28),
        2,
    ),
    _maker(
        "S-ASTERLAB",
        "Asterlab Diagnostics B.V.",
        "NL",
        ("Pieter Visser", "Order desk"),
        "Leiden",
        "EUR",
        (P,),
        (7, 14),
    ),
    _maker(
        "S-LANTERNA",
        "Lanterna Diagnostica S.r.l.",
        "IT",
        ("Giulia Ferraro", "Export office"),
        "Brescia",
        "EUR",
        (E, R),
        (14, 28),
    ),
    _maker(
        "S-FENWICK",
        "Fenwick Assays Ltd",
        "GB",
        ("Oliver Pryce", "Sales office"),
        "Pirbright, Surrey",
        "GBP",
        (P, R),
        (5, 14),
        1,
        "0117 496 0732",
    ),
    _maker(
        "S-HOLLIS",
        "Hollis Laboratory Supplies Ltd",
        "GB",
        ("Sophie Marsh", "Trade counter"),
        "Loughborough",
        "GBP",
        (G, C),
        (3, 7),
        3,
        "0116 496 0255",
    ),
    _maker(
        "S-KRANZ",
        "Kranz Laborbedarf GmbH",
        "DE",
        ("Matthias Kranz", "Vertrieb"),
        "Wiesbaden",
        "EUR",
        (G, C),
        (5, 10),
        2,
    ),
    _maker(
        "S-BRAMBLE",
        "Bramble & Wray Ltd",
        "GB",
        ("Rachel Wray", "Customer services"),
        "Cambridge",
        "GBP",
        (C,),
        (3, 7),
        2,
        "01632 960418",
    ),
    _maker(
        "S-ORVELL",
        "Orvell Scientific Inc.",
        "US",
        ("Kevin Ostrander", "Export desk"),
        "Raleigh, North Carolina",
        "USD",
        (G,),
        (10, 21),
    ),
    _maker(
        "S-DELACOUR",
        "Delacour Chimie SAS",
        "FR",
        ("Hélène Delacour", "Service export"),
        "Rouen",
        "EUR",
        (G,),
        (7, 14),
    ),
    _maker(
        "S-VESTLAB", "Vestlab ApS", "DK", ("Mikkel Holm", "Salg"), "Aarhus", "DKK", (C, G), (7, 14)
    ),
    _maker(
        "S-OKAPI",
        "Okapi Lab Plastics Ltd",
        "GB",
        ("Imran Qureshi", "Sales"),
        "Bolton",
        "GBP",
        (C,),
        (3, 10),
        1,
        "0161 496 0193",
    ),
    Maker(_known("S-CALDER"), "GBP", (Q, K), (21, 56), 4),
    _maker(
        "S-ROTHWELL",
        "Rothwell Instruments Ltd",
        "GB",
        ("Gareth Rothwell", "Sales director"),
        "Stone, Staffordshire",
        "GBP",
        (Q,),
        (14, 42),
        2,
        "020 7946 0311",
    ),
    _maker(
        "S-STEINFELD",
        "Steinfeld Labortechnik GmbH",
        "DE",
        ("Katrin Vogel", "Export"),
        "Göttingen",
        "EUR",
        (Q,),
        (28, 70),
        2,
    ),
    _maker(
        "S-ARKWAY",
        "Arkway Instruments Inc.",
        "US",
        ("Brandon Ruiz", "International sales"),
        "Waltham, Massachusetts",
        "USD",
        (Q,),
        (28, 56),
    ),
    _maker(
        "S-MONTEGRA",
        "Montegra Strumenti S.p.A.",
        "IT",
        ("Paolo Neri", "Ufficio export"),
        "Bologna",
        "EUR",
        (Q,),
        (35, 63),
    ),
    _maker(
        "S-NORRBY",
        "Norrby Kølesystemer A/S",
        "DK",
        ("Astrid Norrby", "Export sales"),
        "Odense",
        "DKK",
        (Q, K),
        (28, 56),
    ),
    _maker(
        "S-GLACIEL",
        "Glaciel Cold Chain SAS",
        "FR",
        ("Julien Masson", "Export"),
        "Nantes",
        "EUR",
        (K,),
        (14, 35),
        2,
    ),
    _maker(
        "S-FROSTMERE",
        "Frostmere Ltd",
        "GB",
        ("Ellie Thornton", "Sales"),
        "Hull",
        "GBP",
        (K,),
        (7, 21),
        2,
        "0114 496 0864",
    ),
    _maker(
        "S-KOELHAVEN",
        "Koelhaven B.V.",
        "NL",
        ("Ruben Bakker", "Order desk"),
        "Rotterdam",
        "EUR",
        (K,),
        (14, 28),
    ),
    _maker(
        "S-ICEBRIDGE",
        "Icebridge Packaging Inc.",
        "US",
        ("Tara Lindgren", "Export sales"),
        "Minneapolis, Minnesota",
        "USD",
        (K,),
        (21, 42),
    ),
    _maker(
        "S-HALVARD",
        "Halvard Thermal Oy",
        "FI",
        ("Emilia Koskinen", "Sales"),
        "Tampere",
        "EUR",
        (K,),
        (21, 35),
    ),
)
"""Thirty manufacturers, the month's three among them."""

PLACES = (
    # Country, city, airport.
    ("VN", "Hanoi", "HAN"),
    ("VN", "Ho Chi Minh City", "SGN"),
    ("TH", "Bangkok", "BKK"),
    ("TH", "Chiang Mai", "CNX"),
    ("LA", "Vientiane", "VTE"),
    ("KH", "Phnom Penh", "PNH"),
    ("MM", "Yangon", "RGN"),
    ("MY", "Kuala Lumpur", "KUL"),
    ("ID", "Jakarta", "CGK"),
    ("ID", "Surabaya", "SUB"),
    ("PH", "Manila", "MNL"),
    ("BD", "Dhaka", "DAC"),
    ("NP", "Kathmandu", "KTM"),
    ("LK", "Colombo", "CMB"),
    ("IN", "Delhi", "DEL"),
    ("IN", "Chennai", "MAA"),
    ("PK", "Lahore", "LHE"),
    ("PK", "Karachi", "KHI"),
    ("BT", "Paro", "PBH"),
    ("TL", "Dili", "DIL"),
)

REQUIREMENTS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "LA": (("Veterinary import permit", "Issued by the national livestock authority"),),
    "KH": (
        ("Import permit", "Issued by the national animal health authority"),
        ("Certificate of origin", "From a UK chamber of commerce"),
    ),
    "MM": (("Import licence", None), ("Certificate of analysis", "One for each batch")),
    "MY": (("Import permit", "Issued by the national veterinary authority"),),
    "ID": (
        ("Import recommendation", "Issued by the national livestock authority"),
        ("Certificate of analysis", "One for each batch"),
    ),
    "PH": (("Veterinary quarantine clearance", None),),
    "BD": (("Import permit", None), ("Certificate of origin", "From a UK chamber of commerce")),
    "NP": (("Veterinary import permit", None),),
    "LK": (("Import licence", "Issued by the national veterinary authority"),),
    "IN": (
        ("Sanitary import permit", "Issued by the national animal husbandry authority"),
        ("Certificate of origin", "From a UK chamber of commerce"),
    ),
    "PK": (("Import permit", None), ("Certificate of analysis", "One for each batch")),
    "BT": (("Import permit", None),),
    "TL": (("Certificate of origin", "From a UK chamber of commerce"),),
}
"""Documents each destination requires, beyond Vietnam's and Thailand's from the setup."""

_STEMS = (
    ("Kor", "Sel", "Mir", "Tun", "Hal", "Ost", "Pen", "Quil"),
    ("Rus", "Tar", "Umb", "Val", "Wen", "Yar", "Zel", "Anth"),
)
_ENDINGS = ("vanta", "aro", "athi", "jaya", "dene")
_KINDS = (
    ("{} Veterinary Diagnostic Centre", PaymentTerms.NET_60),
    ("{} Agrovet Trading Co.", PaymentTerms.NET_30),
    ("{} Livestock Development Board", PaymentTerms.LETTER_OF_CREDIT),
    ("{} Animal Health Supplies", PaymentTerms.ADVANCE),
    ("{} Institute of Veterinary Science", PaymentTerms.NET_90),
    ("{} Poultry Health Cooperative", PaymentTerms.NET_30),
    ("{} Smallholder Livestock Programme", PaymentTerms.NET_30),
    ("{} Laboratory Services", PaymentTerms.NET_60),
)
_PEOPLE = (
    ("Anan", "Sukprasert"),
    ("Linh", "Pham"),
    ("Dewi", "Hartono"),
    ("Rafael", "Santos"),
    ("Farhana", "Akter"),
    ("Suman", "Thapa"),
    ("Nimal", "Perera"),
    ("Kavya", "Raman"),
    ("Imran", "Malik"),
    ("Sonam", "Wangchuk"),
    ("Aye", "Thandar"),
    ("Sokha", "Chea"),
    ("Mei Ling", "Tan"),
    ("Joao", "Belo"),
    ("Khamla", "Phommachanh"),
    ("Arjun", "Mehta"),
)
_ROLES = ("Procurement officer", "Laboratory manager", "Accounts payable", "Programme coordinator")


def _buyers() -> list[Buyer]:
    first = (*_STEMS[0], *_STEMS[1])
    made: list[Buyer] = []
    for index in range(78):
        stem = first[index % 16] + _ENDINGS[index // 16]
        pattern, terms = _KINDS[index % len(_KINDS)]
        name = pattern.format(stem)
        country, city, airport = PLACES[index % len(PLACES)]
        domain = f"{stem.lower()}.example"
        contacts = tuple(
            Contact(
                f"{given} {family}",
                _ROLES[(index + n) % len(_ROLES)],
                f"{given.lower().replace(' ', '')}.{family.lower()}@{domain}",
            )
            for n, (given, family) in enumerate(
                (_PEOPLE[index % 16], _PEOPLE[(index * 7 + 3) % 16])[: 2 if index % 3 == 0 else 1]
            )
        )
        site = (
            (("Delivery", (f"{name}, central store", f"{20 + index * 7 % 160} {stem} Road", city)),)
            if index % 2 == 0
            else ()
        )
        made.append(
            Buyer(
                Organisation(
                    f"C-{stem.upper()}",
                    name,
                    country,
                    contacts,
                    (("Billing", (name, f"PO Box {1100 + index * 37}", city)), *site),
                ),
                city,
                airport,
                "EUR" if index % 6 == 3 else "USD",
                terms,
            )
        )
    return made


BUYERS: tuple[Buyer, ...] = (
    Buyer(_known("C-NVDL"), "Hanoi", "HAN", "USD", PaymentTerms.NET_30),
    Buyer(_known("C-DLD"), "Bangkok", "BKK", "EUR", PaymentTerms.LETTER_OF_CREDIT),
    *_buyers(),
)
"""Eighty customers, the month's two among them."""

PROGRAMMES = (
    "LHSP-25-031",
    "ADRP-24-208",
    "SPHI-25-014",
    "VLRP-24-092",
    "CDCP-25-077",
    "PPRE-24-150",
    "FMDC-25-006",
)
"""References of the programmes that pay for orders: invented, as their funders are."""

END_USERS = (
    "District veterinary laboratories, northern region",
    "Provincial vaccination teams",
    "University teaching laboratory",
    "Regional disease investigation centre",
    "Smallholder poultry vaccinators",
    "Border inspection posts",
)

AIRLINES = (
    # Name, waybill prefix, flight number prefix.
    ("Kittiwake Air Cargo", "917", "KWC"),
    ("Meridian Skyfreight", "921", "MSF"),
    ("Saffron Air Cargo", "934", "SFA"),
    ("Northlight Cargo Airlines", "948", "NLC"),
)
"""Invented airlines, with waybill prefixes chosen for them."""

FORWARDERS = ("Thamesbridge Forwarding", "Albury Freight Services", "Copperleaf Logistics")
HAULIERS = ("Pennine Express Haulage", "Severnside Couriers", "Langdale Transport")
