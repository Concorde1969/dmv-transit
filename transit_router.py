import requests

routes_url = "https://transit.land/api/v2/rest/routes/?feed_onestop_id=f-dqc-wmata~rail"
trips_url = "https://transit.land/api/v2/rest/routes/{route_key}/trips"
stops_url = "https://transit.land/api/v2/rest/stops?feed_onestop_id=f-dqc-wmata~rail"

headers = {
    "apikey": "iwa_live_tlv2api_7cda1feae73bd9232e67841f7019e69229ff07cfd885b9a74pxNsw"
        }

routes = requests.get(routes_url, headers = headers)
stops = requests.get(stops_url, headers = headers)

print(routes.json())
print(stops.json())

"f-dqc-wmata~bus"
"f-dqc-virginiarailwayexpress"
"f-dqcj-fairfaxconnector"
"f-dqcjj-arlingtontransit"
"f-dqchv-alexandriatransitcompanydash"
"f-dqcj1-fairfaxcue"
"f-loudouncounty~va~us"
"f-dqch-prtc"
"f-utel~uiuc~intercity~bus"
"f-virginia~breeze"