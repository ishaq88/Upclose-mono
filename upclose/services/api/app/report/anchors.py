"""Fixed approximate pin anchors for the generic five-view car diagram.

Coordinates are positions on a generic drawing (every view is a 240x120 box). They are
not measured locations on any real vehicle. Lookup order is component, then
(zone, subzone), then zone. Changing any coordinate requires bumping ANCHOR_VERSION.
"""

from dataclasses import dataclass

from app.contracts.enums import ComponentId, Subzone, Zone

ANCHOR_VERSION = "report-pin-anchors-v1"
VIEW_WIDTH = 240
VIEW_HEIGHT = 120
VIEWS = ("top", "left", "right", "front", "rear")
VIEW_LABELS = {"top": "Top", "left": "Driver side", "right": "Passenger side", "front": "Front", "rear": "Rear"}


@dataclass(frozen=True)
class Anchor:
    view: str
    x: float
    y: float


def _a(view: str, x: float, y: float) -> Anchor:
    return Anchor(view, x, y)


def _mirror(anchor: Anchor) -> Anchor:
    """Right-side view: same generic side profile, front toward the left of the drawing."""
    return Anchor("right", VIEW_WIDTH - anchor.x, anchor.y)


# Driver-side profile (front of the car toward the right of the drawing).
_LEFT = {
    "rocker": _a("left", 120, 82),
    "front_door": _a("left", 148, 66),
    "rear_door": _a("left", 100, 66),
    "fender": _a("left", 204, 68),
    "quarter_panel": _a("left", 36, 68),
    "hood": _a("left", 196, 58),
    "trunk": _a("left", 30, 62),
    "roof": _a("left", 110, 32),
    "windshield": _a("left", 170, 46),
    "front_bumper": _a("left", 220, 78),
    "rear_bumper": _a("left", 20, 78),
    "tire_front": _a("left", 180, 86),
    "tire_rear": _a("left", 60, 86),
    "zone": _a("left", 120, 60),
}

COMPONENT_ANCHORS: dict[ComponentId, Anchor] = {
    ComponentId.DRIVER_ROCKER: _LEFT["rocker"],
    ComponentId.PASSENGER_ROCKER: _mirror(_LEFT["rocker"]),
    ComponentId.DRIVER_FRONT_DOOR: _LEFT["front_door"],
    ComponentId.DRIVER_REAR_DOOR: _LEFT["rear_door"],
    ComponentId.PASSENGER_FRONT_DOOR: _mirror(_LEFT["front_door"]),
    ComponentId.PASSENGER_REAR_DOOR: _mirror(_LEFT["rear_door"]),
    ComponentId.TIRE_FL: _LEFT["tire_front"],
    ComponentId.TIRE_RL: _LEFT["tire_rear"],
    ComponentId.TIRE_FR: _mirror(_LEFT["tire_front"]),
    ComponentId.TIRE_RR: _mirror(_LEFT["tire_rear"]),
    ComponentId.WINDSHIELD: _a("top", 166, 60),
    ComponentId.HOOD: _a("top", 192, 60),
    ComponentId.ROOF: _a("top", 128, 60),
    ComponentId.TRUNK: _a("top", 50, 60),
    ComponentId.FRONT_BUMPER: _a("front", 120, 84),
    ComponentId.REAR_BUMPER: _a("rear", 120, 84),
}

_SIDE_SUBZONE_KEYS = {
    Subzone.ROCKER: "rocker",
    Subzone.DOOR: "front_door",
    Subzone.FENDER: "fender",
    Subzone.QUARTER_PANEL: "quarter_panel",
    Subzone.HOOD: "hood",
    Subzone.TRUNK: "trunk",
    Subzone.ROOF: "roof",
    Subzone.WINDSHIELD: "windshield",
    Subzone.TIRE: "tire_front",
}

SUBZONE_ANCHORS: dict[tuple[Zone, Subzone], Anchor] = {}
for _subzone, _key in _SIDE_SUBZONE_KEYS.items():
    SUBZONE_ANCHORS[(Zone.DRIVER_SIDE, _subzone)] = _LEFT[_key]
    SUBZONE_ANCHORS[(Zone.PASSENGER_SIDE, _subzone)] = _mirror(_LEFT[_key])
SUBZONE_ANCHORS.update({
    (Zone.FRONT_REAR_BODY, Subzone.BUMPER): _a("front", 120, 84),
    (Zone.FRONT_REAR_BODY, Subzone.HOOD): _a("top", 192, 60),
    (Zone.FRONT_REAR_BODY, Subzone.TRUNK): _a("top", 50, 60),
    (Zone.FRONT_REAR_BODY, Subzone.WINDSHIELD): _a("top", 166, 60),
    (Zone.FRONT_REAR_BODY, Subzone.ROOF): _a("top", 128, 60),
    (Zone.DASH_ELECTRICAL, Subzone.DASH): _a("top", 150, 60),
    (Zone.INTERIOR, Subzone.INTERIOR): _a("top", 96, 60),
    (Zone.ENGINE_BAY, Subzone.ENGINE_BAY): _a("top", 192, 48),
    (Zone.UNDERBODY, Subzone.UNDERBODY): _a("top", 118, 88),
    (Zone.TIRES, Subzone.TIRE): _a("front", 120, 100),
})

ZONE_ANCHORS: dict[Zone, Anchor] = {
    Zone.DRIVER_SIDE: _LEFT["zone"],
    Zone.PASSENGER_SIDE: _mirror(_LEFT["zone"]),
    Zone.FRONT_REAR_BODY: _a("front", 120, 66),
    Zone.TIRES: _a("front", 120, 100),
    Zone.DASH_ELECTRICAL: _a("top", 150, 60),
    Zone.ENGINE_BAY: _a("top", 192, 48),
    Zone.UNDERBODY: _a("top", 118, 88),
    Zone.INTERIOR: _a("top", 96, 60),
}


def anchor_for(zone: Zone, subzone: Subzone | None, component: ComponentId | None) -> Anchor:
    """Component anchor, else subzone anchor, else the zone-level anchor."""
    if component is not None and component in COMPONENT_ANCHORS:
        return COMPONENT_ANCHORS[component]
    if subzone is not None and (zone, subzone) in SUBZONE_ANCHORS:
        return SUBZONE_ANCHORS[(zone, subzone)]
    return ZONE_ANCHORS[zone]
