##################################
# Configurations
BEDS_MIN = 2  # min number of beds
PRICE_MAX = 350000  # max price in GBP
RADIUS = 1  # search radius in miles
PAGINATION_SIZE = 100  # set to max page size, to reduce number of calls

# q: your search in zooplas website
# link: the link you get once you search
pages = [
    {'link': 'www.zoopla.co.uk/for-sale/property/station/rail/esher/',
     'q': "Esher Station, Surrey"},
    {'link': 'www.zoopla.co.uk/for-sale/houses/kingston-vale/',
     'q': "Kingston Vale, London"}]

##################################
# CODE

default_params = {"beds_min": BEDS_MIN,
                  "price_max": PRICE_MAX,
                  "radius": RADIUS,
                  "page_size": PAGINATION_SIZE
                  }

from bs4 import BeautifulSoup
from urllib.parse import urlencode, urlunparse
import json
import requests
from ratelimit import limits
from time import sleep, time
from geopy.geocoders import Nominatim

from datetime import datetime
import re
from ratelimit.exception import RateLimitException
from selenium import webdriver
from selenium.webdriver import FirefoxOptions
from os import environ, path, curdir

drivers_path = path.join(path.abspath(curdir), "drivers")
environ["PATH"] += drivers_path

# will be needed for reverse geolocation
locator = Nominatim(user_agent='myGeocoder')

# @limits: making sure I don't DDOS Zoopla's website!
@limits(calls=1, period=1)
def _get_webpage_soup(link, q, page_num):
    """ Given a link, q, and page, it returns the page SOUP for a Zoopla search page
    """
    params = default_params
    params['pn'] = page_num
    params['q'] = q
    url = urlunparse(['https', link, '', '', urlencode(params), ''])
    print(url)
    opts = FirefoxOptions()
    opts.add_argument("--headless")
    driver = webdriver.Firefox(firefox_options=opts, executable_path=drivers_path + '/geckodriver')
    driver.get(str(url))
    soup = BeautifulSoup(driver.page_source)
    driver.close()
    return soup


def _get_listing_ids(soup):
    """ Given a results page soup, extracts all the property IDs in that page
    """
    id_list = []
    for itm in soup.find_all('a', attrs={"data-testid": "listing-details-link"}):
        id_list.append(itm.get('href').split('details/')[1].split('?')[0])
    return id_list


def get_main_page_listing(page_cnf):
    max_loop = 50
    page = 1
    mx_page = 99
    list_ids = []

    while max_loop > 0:
        print("Getting page", page)
        page_cnf['page_num'] = page
        try:
            soup = _get_webpage_soup(**page_cnf)
        except RateLimitException:
            print("You broke the speed limit boy! Slow down!")
            sleep(2)
            continue
        if not soup:
            print('There was no soup.... break')
            break
        _ids = _get_listing_ids(soup)
        list_ids += _ids
        if len(_ids) == 0:
            print('Results are no more! Break after ', len(list_ids), ' records')
            break
        page += 1
        max_loop -= 1
    if max_loop == 0:
        raise Exception(f"Something is clearly wrong... can't have {mx_page}+ pages can it?")

    return [(page_cnf['q'], i) for i in list_ids]


def label_to_date(label: str) -> datetime:
    return datetime.strptime(re.sub(r'(\d)(st|nd|rd|th)', r'\1', label), '%d %b %Y')


def soup_get_price_history(soup) -> dict:
    # for price_item in soup.find_all('div', attrs={"class": 'dp-price-history__item'}):
    rows = []

    price_history_items = soup.find_all('div', attrs={"class": 'dp-price-history__item'})
    for itm in price_history_items:
        _val = []
        for i in itm.select('span[class*="dp-price-history"]'):
            _val += [i.text.strip('\n').strip('\\n').strip()]
        rows += [(label_to_date(_val[0]).date().strftime("%Y%m%d"),
                  int(_val[1].strip('£').replace(',', '')),
                  _val[2]
                  )
                 ]
    return rows


def get_soup_text(soup_, type_, class_, char_erase=''):
    snip = soup_.find(type_, class_)
    if not snip:
        return None
    else:
        txt = snip.text.strip('\n').strip()
        for ch in char_erase:
            txt = txt.replace(ch, '')
        return txt

@limits(calls=3, period=1)
def get_property_details(property_id, location, soup):
    house_data = dict({'id': property_id, 'location': location})

    # get price history
    house_data['price_history'] = soup_get_price_history(soup)
    for price_hist in house_data['price_history']:
        if price_hist[2] == 'First listed':
            house_data['first_listed'] = price_hist[0]

    # side summary
    soup_side_summary = soup.find('article', class_='dp-sidebar-wrapper__summary')
    house_data['headline'] = get_soup_text(soup_side_summary, 'h1', 'ui-property-summary__title ui-title-subgroup')
    house_data['partial_address'] = get_soup_text(soup_side_summary, 'h2', 'ui-property-summary__address')
    house_data['price'] = get_soup_text(soup_side_summary, 'p', 'ui-pricing__main-price ui-text-t4', '£,')

    # description
    soup_details = soup.find('section', id='property-details-tab')
    house_data['description'] = get_soup_text(soup_details, 'div', 'dp-description__text')

    # get summary data (bedrooms, bathrooms, etc)
    for feat_soup in soup_details.find_all('span', class_='dp-features-list__text'):
        (key, val) = (' '.join(feat_soup.text.rstrip('s').split(' ')[1:]), feat_soup.text.rstrip('s').split(' ')[0])
        if not key:
            key = val
            val = True
        house_data[key.replace(' ', '_').lower()] = val

    # number of views (last 30 days, and from listing)
    for view in soup_details.find_all('p', class_='dp-view-count__legend'):
        parts = view.text.replace('\n', '').strip().split(':')
        house_data['views_' + parts[0].replace(' ', '_').lower()] = int(re.findall('\d+', parts[1])[0])

    # sat nav location
    map_soup = soup.find('img', class_='ui-static-map__img')
    if map_soup:
        loc = map_soup['data-src'].split('/maps/markers/pin-default.png%7C')
        if len(loc) > 1:
            loc = loc[1].split('&')[0].split(',')
            house_data['lat'] = loc[0]
            house_data['long'] = loc[1]

            # reverse geolocation
            try:
                location = locator.reverse(f"{house_data['lat']}, {house_data['long']}")
                addr = location.raw.get('address')
                if addr:
                    house_data['road'] = addr.get('road')
                    house_data['postcode'] = addr.get('postcode')
                else:
                    print('No address found on this one...')

            except TypeError:
                print('Failed to get an address (but it is fine)')
        else:
            print('Could not find map!')
    return house_data


list_ids = []
been_there_done_that = set()
start_time = time()
for page_cnf in pages:
    lap_time = time()
    been_there_done_that = been_there_done_that.union(set([v for i, v in list_ids]))
    print("$" * 40)
    print('Getting', page_cnf["q"])
    _retry = 0
    while True:
        _list = []
        try:
            _list += [i
                      for i in get_main_page_listing(page_cnf)
                      if i not in list_ids]
            break
        except Exception as e:
            if _retry > 2:
                raise e
            print("That is unexpected...")
            print(e)
            sleep(2)
            _retry += 1
    list_ids += [(i, v)
                 for i, v in _list
                 if v not in been_there_done_that]
    print(
        f"You fast homey! {len(list_ids)} properties in {round(time() - lap_time, 1)}s (T: {round(time() - start_time, 2)})")
    sleep(1)
print(f"Done, got {len(list_ids)} houses to lookup")

# Get property details
data = []
TOTAL = len(list_ids)

i = 0
start_time = time()
for (location, property_id) in list_ids[max(i - 1, 0):]:
    lap_time = time()
    i += 1
    url = urlunparse(['http', 'www.zoopla.co.uk', f'for-sale/details/{property_id}', '', '', ''])

    soup = BeautifulSoup(requests.get(url).content, 'html')
    print("Loading", url, f"{i} of {TOTAL}", end='')

    house_data = get_property_details(property_id, location, soup)

    print(f" ( {round(time() - lap_time, 1)}s , T: {round(time() - start_time, 2)})")
    data += [house_data]
    sleep(0.1)

print('DONE! ', len(data), f'houses extracted in T: {round((time() - start_time) / 60)}min)')

# write all data to its final destination
with open('data/zoopla_' + datetime.now().strftime("%Y%m%d") + '.jsonl', 'w') as outfile:
    for entry in data:
        outfile.write(json.dumps(dict(entry)))
        outfile.write('\n')
