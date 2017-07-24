
import re
from datetime import datetime, timedelta
import json
import scrapy
from scrapy.linkextractors import LinkExtractor

from ..items import Publication
from ..utils import extract_documents
from ..utils import strip_diacritics
from ..utils import guess_initiative_type

INDEX_URL = 'http://mts.ro/proiecte-legislative-in-dezbatere-publica/'

TYPE_RULES = [
    ("lege", "LEGE"),
    ("hotarare de guvern", "HG"),
    ("hotarare a guvernului", "HG"),
    ("hotarare", "HG"),
    ("hg", "HG"),
    ("ordonanta de guvern", "OG"),
    ("ordonanta de urgenta", "OUG"),
    ("ordin de ministru", "OM"),
    ("ordinul", "OM"),
]

CONTACT_TEL_FAX_PATTERN = re.compile(r'((fax|telefon|tel)[^\d]{1,10}(\d(\d| |\.){8,11}\d))')
CONTACT_EMAIL_PATTERN = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-]{2,5})")

# matches lines similar to "Data limită pentru primirea de propuneri/observaţii (10 zile de la publicare): 07 aprilie 2017"
# and
# "Data limită pentru primirea de propuneri/opinii/sugestii : 26.09.2016"

#TODO some have this format:
#Pentru eficientizarea centralizării propunerilor/observațiilor de modificare, vă rugăm să aveţi amabilitatea de a le transmite în termen de 20 zile

FEEDBACK_DEADLINE_INFO_PATTERN = re.compile(r'data limita.*(.*\(.*de la publicare\))*.*((\d\d?\.\d\d?\.20\d\d)|(\d\d?\s[a-z]+\s20\d\d))*')
FEEDBACK_DEADLINE_DATE_PATTERN = re.compile(r'(\d\d?\.\d\d?\.20\d\d)|(\d\d?\s[a-z]+\s20\d\d)')
FEEDBACK_DEADLINE_DAYS_PATTERN = re.compile(r'\(.*de la publicare\)')

FEEDBACK_DATE_FORMATS = ['%d %B %Y', '%d.%m.%Y']

def text_from(sel):
    return sel.xpath('string(.)').extract_first().strip()

class TineretSpider(scrapy.Spider):

    name = "tineret"
    start_urls = [INDEX_URL]

    def parse(self, response):
        articleLinks = LinkExtractor(restrict_css='div.main > div.article')
        pages = articleLinks.extract_links(response)
        for page in pages:
            yield scrapy.Request(page.url, callback=self.parse_article)

    def parse_article(self, response):
        article_node = response.css('div.main>div.article')

        title = text_from(article_node.css('h3.article-title'))
        title = self.clean_title(title)

        # clean up most of the title before checking publication type
        publication_text = title.lower().strip()
        publication_type = "OTHER"
        stop_pos = re.search(r'(pentru|privind)', publication_text)
        if stop_pos:
            publication_text_short = publication_text[0:stop_pos.start()]
            publication_type = guess_initiative_type(publication_text_short, TYPE_RULES)

        text_date = text_from(article_node.css('span.date'))
        date, date_obj = self.parse_date(text_date)

        content_node = article_node.css('div.article-content')

        description = text_from(content_node)
        description_without_diacritics = strip_diacritics(description)

        documents = [
            {
                'type': doc['type'],
                'url': response.urljoin(doc['url']),
            } for doc in
            extract_documents(content_node.css('a'))
        ]
        json_documents = json.dumps(documents)

        feedback_days, feedback_date = self.get_feedback_times(description_without_diacritics, date_obj)

        contact = self.get_contacts(description_without_diacritics)
        json_contact = json.dumps(contact)

        publication = Publication(
            institution = 'tineret',
            identifier = self.slugify(title)[0:127],
            type = publication_type,
            date = date,
            title = title,
            description = description,
            documents = json_documents,
            contact = json_contact,
            feedback_days = feedback_days,
            max_feedback_date = feedback_date
        )

        if feedback_days == None:
            print(publication)

    def slugify(self, text):
        text = strip_diacritics(text).lower()
        return re.sub(r'\W+', '-', text)

    def get_feedback_times(self, text, publish_date):
        fdbk_days = None
        fdbk_date = None

        text = text.strip().lower()

        phrase = re.search(FEEDBACK_DEADLINE_INFO_PATTERN, text)

        if phrase:
            #check if date is present
            date = re.search(FEEDBACK_DEADLINE_DATE_PATTERN, phrase.group(0))
            if date:
                date = date.group(0)
                for format in FEEDBACK_DATE_FORMATS:
                    try:
                        result = datetime.strptime(date, format)
                        if result:
                            fdbk_date = result
                    except ValueError:
                        pass

            # check if number of days is present
            days = re.search(FEEDBACK_DEADLINE_DAYS_PATTERN, phrase.group(0))
            if days:
                days_text = days.group(0).replace("(", "").split(" ")
                try:
                    days_int = int(days_text[0])
                    fdbk_days = days_int
                except ValueError:
                    pass

        if fdbk_days and not fdbk_date:
            #compute date
            fdbk_date = (publish_date + timedelta(days=fdbk_days)).date().isoformat()

        if not fdbk_days and fdbk_date:
            #compute days
            days_diff = fdbk_date - publish_date
            fdbk_days = days_diff.days

        return fdbk_days, fdbk_date


    def get_contacts(self, text):
        text = text.strip().lower()

        contact = {}

        emails = re.findall(CONTACT_EMAIL_PATTERN, text)
        contact['email'] = list(set(emails))

        numbers = re.findall(CONTACT_TEL_FAX_PATTERN, text)
        for number in numbers:
            key = number[1]
            value = number[2].replace(' ','').replace('.', '')
            if key in contact:
                contact[key].push(value)
            else:
                contact[key] = [value]

        for k,v in contact.items():
            contact[k] = ','.join(v)

        return contact

    def parse_date(self, text):
        try:
            date_obj = datetime.strptime(text, '%d.%m.%Y')
            date = date_obj.date().isoformat()
        except ValueError:
            date = None
        return date, date_obj

    def clean_title(self, text):
        """
        Remove possible extra spaces in title (ex. HOTĂRÂRE spelled as H O T Ă R Â R E)
        """
        idx = 0
        parts = text.split()
        for i in range(len(parts)):
            if len(parts[i]) > 1:
                idx = i
                break

        text = '%s %s' % (''.join(parts[:idx]), ' '.join(parts[idx:]))
        return text
