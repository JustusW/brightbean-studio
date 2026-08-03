"""The application's own URLs, plus something that serves uploaded media.

WITHOUT THIS, EVERY MEDIA FEATURE IS INVISIBLE. A person attaches a picture,
the upload succeeds, the composer records it and the platform preview lays out
a proper post card - and the picture itself is drawn as a broken image, in the
composer and in the preview both. Chased through the browser it turned out the
element carried a real source,

    /media/media_library/2026/08/brightbean-e2e-latte_p29ya4K.png

with naturalWidth 0: the file had been stored, and nothing was serving it. No
HTTP refusal was recorded and the Content Security Policy blocked nothing, so
there was nothing to see anywhere except in the picture.

A deployment does not have this problem: media goes to object storage and is
served from there, which is what django-storages is in the requirements for.
The e2e harness keeps files on local disk, and Django only serves those when
DEBUG is on - which this suite deliberately leaves off, because a debug server
answers with error pages production never shows.

So the harness serves them itself, explicitly, and only in the harness: the
settings module for the e2e suite points ROOT_URLCONF here. The application's
own URL configuration is included unchanged and untouched - this adds one
route to the end of it and nothing else.
"""

from django.conf import settings
from django.urls import re_path
from django.views.static import serve

from config.urls import urlpatterns as the_applications_own_urls

urlpatterns = [
    *the_applications_own_urls,
    # django.conf.urls.static.static() would be the obvious way to write this
    # and it is the wrong one: it returns an EMPTY list unless DEBUG is set, so
    # it would have quietly done nothing here and left the pictures broken.
    re_path(
        rf"^{settings.MEDIA_URL.lstrip('/')}(?P<path>.*)$",
        serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
