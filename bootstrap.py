import requests
from bs4 import BeautifulSoup as bs4
import json
import re

# Notes: add md5 hash?
# Notes: add redundancy check for external (non BLFS) package urls
# Notes: add requirements.txt, add README.md
# Notes: remove SSL error

'''
JSON scheme
{
'Package Name': {                                         ---string
        'Version': '1.0.0',                               ---string
        'URL': ['http://asdfasdfasdf','http://patch],     ---array (includes patches urls)
        'Deps': {                                         ---dict or None
            'Required': ['first', 'second'],              ---array or None
            'Recommended': ['first', 'second'],           ---array or None
            'Optional': ['first', 'second']               ---array or None
            }, 
        'Commands': ['Installation commands']             ---array
        }
}
'''

baseUrl = 'https://www.linuxfromscratch.org/blfs/view/stable/longindex.html' # URL containing all package urls

links = []
scheme = {}


def packageCollect(package, tagClass, tag):
    version = package.find(tag, class_=tagClass).text.strip()  # string of version
    name = package.find(tag, class_=tagClass).find('a').get('id')  # string of name

    deps = {'required': [], 'recommended': [], 'optional': []}  # remove redundancy?
    for y in deps:
        for i in package.find_all('p', class_=y):
            for j in i.find_all('a', title=True, class_='xref'):  # grab blfs deps
                deps[y].append(re.sub(r'\n\s+', ' ', j['title']))

            for j in i.find_all('a', class_='ulink'):  # grab external deps
                deps[y].append(re.sub(r'\n\s+', ' ', j.text))
                scheme[re.sub(r'\n\s+', ' ', j.text)] = {'url': j['href']} # manually add url to scheme 

    commands = []
    for i in package.find_all('kbd', class_='command'):  # remove whitespace
        commands.append(re.sub("\s+\\\\\\n\s+", ' ', i.text))  # does not work with 

    urls = []
    if package.find('div', class_='itemizedlist'):
        for i in package.find('div', class_='itemizedlist').find_all('a', class_='ulink'): # if package has urls add to array
            urls.append(i['href'])

    print("Downloading info for {0}".format(name))
    scheme[name] = {'Version': version, 'url': urls, 'Dependencies': deps, 'Commands': commands}

res = requests.get(baseUrl, verify=False) # Begin...
soup = bs4(res.text, 'html.parser')
el = soup.find('a', attrs={"id":"package-index"}).parent.next_sibling.next_sibling
print("Collecting base URLs....")
for i in el.find_all('a', href=True): # for every url... check if has hashtag... if not add to array
    if not '#' in i['href']:
        links.append('https://www.linuxfromscratch.org/blfs/view/stable/' + i['href'])
        
        

for i in links:
    res = requests.get(i.rstrip(), verify=False)
    soup = bs4(res.text, 'html.parser')  # get webpage contents

    if soup.find_all('div', class_='sect2'):  # if soup is module instead of std package
        for module in soup.find_all('div', class_='sect2'):
            if module.find_all('div', class_='package'):  # limit to modules only
                packageCollect(module, "sect2", "h2") # call function on module
    else:
        packageCollect(soup, "sect1", "h1") # call function on std package

with open('dependencies.json', 'w+') as j: # dump info to json file
    json.dump(scheme, j)
