'''Tests asynchronous HttpClient.'''
from pulsar import send, is_failure, NOT_DONE
from pulsar.apps.test import unittest
from pulsar.utils import httpurl
from pulsar.utils.events import Listener
from pulsar.utils.httpurl import to_bytes, urlencode
from pulsar.apps.http import HttpClient, TooManyRedirects, HttpResponse,\
                                HTTPError


class TestHttpClientBase:
    app = None
    with_proxy = False
    proxy_app = None
    timeout = 10
    
    @classmethod
    def setUpClass(cls):
        # Create the HttpBin server by sending this request to the arbiter
        from examples.proxyserver.manage import server as pserver
        from examples.httpbin.manage import server
        concurrency = cls.cfg.concurrency
        s = server(bind='127.0.0.1:0', concurrency=concurrency,
                   name='httpbin-%s' % cls.__name__.lower(),
                   keep_alive=30)
        cls.app = yield send('arbiter', 'run', s)
        cls.uri = 'http://%s:%s' % cls.app.address
        if cls.with_proxy:
            s = pserver(bind='127.0.0.1:0', concurrency=concurrency,
                        name='proxyserver-%s' % cls.__name__.lower())
            outcome = send('arbiter', 'run', s)
            yield outcome
            cls.proxy_app = outcome.result
            cls.proxy_uri = 'http://{0}:{1}'.format(*cls.proxy_app.address)
        
    @classmethod
    def tearDownClass(cls):
        if cls.app is not None:
            yield send('arbiter', 'kill_actor', cls.app.name)
        if cls.proxy_app is not None:
            yield send('arbiter', 'kill_actor', cls.proxy_app.name)
        
    def client(self, timeout=None, **kwargs):
        timeout = timeout or self.timeout
        if self.with_proxy:
            kwargs['proxy_info'] = {'http': self.proxy_uri}
        return HttpClient(timeout=timeout, **kwargs)
    
    def _check_pool(self, http, response, available=1, processed=1, created=1,
                    pools=1):
        self.assertEqual(len(http.connection_pools), pools)
        if pools:
            pool = http.connection_pools[response.current_request.key]
            #self.assertEqual(pool.concurrent_connections, 0)
            self.assertEqual(pool.received, created)
            self.assertEqual(pool.available_connections, available)
            if available == 1:
                connection = tuple(pool._available_connections)[0]
                self.assertEqual(connection.processed, processed)
            
    def httpbin(self, *suffix):
        if suffix:
            return self.uri + '/' + '/'.join(suffix)
        else:
            return self.uri
        
    def available_on_error(self):
        return 1 if self.proxy_app else 0
    
    
class TestHttpClient(TestHttpClientBase, unittest.TestCase):
    
    def test_http10(self):
        '''By default HTTP/1.0 close the connection if no keep-alive header
was passedby the client.'''
        http = self.client(version='HTTP/1.0')
        http.headers.clear()
        self.assertEqual(http.version, 'HTTP/1.0')
        response = yield http.get(self.httpbin()).on_finished
        self.assertEqual(response.headers['connection'], 'close')
        self.assertEqual(str(response), '200 OK')
        self._check_pool(http, response, available=0)
    
    def test_http11(self):
        '''By default HTTP/1.1 keep alive the connection if no keep-alive header
was passed by the client.'''
        http = self.client()
        http.headers.clear()
        self.assertEqual(http.version, 'HTTP/1.1')
        response = yield http.get(self.httpbin()).on_finished
        self.assertEqual(response.headers['connection'], 'keep-alive')
        self._check_pool(http, response)
  
    def testClient(self):
        http = self.client(max_redirects=5, timeout=33)
        self.assertTrue('accept-encoding' in http.headers)
        self.assertEqual(http.timeout, 33)
        self.assertEqual(http.version, 'HTTP/1.1')
        self.assertEqual(http.max_redirects, 5)
        if self.with_proxy:
            self.assertEqual(http.proxy_info, {'http': self.proxy_uri})
  
    def test_200_get(self):
        http = self.client()
        response = yield http.get(self.httpbin()).on_finished
        self._check_pool(http, response)
        self.assertEqual(str(response), '200 OK')
        self.assertEqual(repr(response), 'HttpResponse(200 OK)')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.response, 'OK')
        self.assertTrue(response.get_content())
        self.assertEqual(response.url, self.httpbin())
        self._check_pool(http, response)
        response = yield http.get(self.httpbin('get')).on_finished
        self.assertEqual(response.status_code, 200)
        self._check_pool(http, response, processed=2)
        
    def test_HttpResponse(self):
        r = HttpResponse(None)
        self.assertEqual(r.current_request, None)
        self.assertEqual(str(r), '<None>')
        
    def test_400_and_get(self):
        '''Bad request 400'''
        http = self.client()
        listener = Listener('post_request', 'connection_lost')
        self.assertFalse(listener['post_request'])
        response = yield http.get(self.httpbin('status', '400')).on_finished
        N = len(listener['post_request'])
        self.assertTrue(N)
        self._check_pool(http, response, available=self.available_on_error())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.response, 'Bad Request')
        self.assertTrue(response.get_content())
        self.assertRaises(HTTPError, response.raise_for_status)
        # Make sure we only have one connection after a valid request
        response = yield http.get(self.httpbin('get')).on_finished
        self.assertTrue(len(listener['post_request']) > N)
        self.assertEqual(response.status_code, 200)
        if self.proxy_app:
            self._check_pool(http, response, created=1, processed=2)
        else:
            self._check_pool(http, response, created=2)
        
    def test_large_response(self):
        http = self.client(timeout=60)
        response = yield http.get(self.httpbin('getsize/600000')).on_finished
        self.assertEqual(response.status_code, 200)
        data = response.content_json()
        self.assertEqual(data['size'], 600000)
        self.assertEqual(len(data['data']), 600000)
        self.assertFalse(response.parser.is_chunked())
       
    def test_redirect(self):
        http = self.client()
        response = yield http.get(self.httpbin('redirect', '1')).on_finished
        self.assertEqual(response.status_code, 200)
        history = response.history
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].url.endswith('/redirect/1'))
    
    def test_too_many_redirects(self):
        http = self.client()
        response = http.get(self.httpbin('redirect', '5'), max_redirects=2)
        # do this so that the test suite does not fail on the test
        yield response.on_finished.add_errback(lambda f: [f])
        r = response.on_finished.result[0]
        self.assertTrue(is_failure(r))
        self.assertTrue(isinstance(r.trace[1], TooManyRedirects))
        history = response.history
        self.assertEqual(len(history), 2)
        self.assertTrue(history[0].url.endswith('/redirect/5'))
        self.assertTrue(history[1].url.endswith('/redirect/4'))
        
    def test_200_get_data(self):
        http = self.client()
        response = yield http.get(self.httpbin('get'),
                                  data={'bla': 'foo'}).on_finished
        self.assertEqual(response.status_code, 200)
        self._check_pool(http, response)
        self.assertEqual(response.response, 'OK')
        result = response.content_json()
        self.assertEqual(result['args'], {'bla':['foo']})
        self.assertEqual(response.url,
                self.httpbin(httpurl.iri_to_uri('get',{'bla': 'foo'})))
        
    def test_200_gzip(self):
        http = self.client()
        response = yield http.get(self.httpbin('gzip')).on_finished
        self.assertEqual(response.status_code, 200)
        self._check_pool(http, response)
        self.assertEqual(response.response, 'OK')
        content = response.content_json()
        self.assertTrue(content['gzipped'])
        if 'content-encoding' in response.headers:
            self.assertTrue(response.headers['content-encoding'], 'gzip')

    def test_404_get(self):
        '''Not Found 404'''
        http = self.client()
        response = yield http.get(self.httpbin('status', '404')).on_finished
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.response, 'Not Found')
        if self.proxy_app:
            self.assertTrue(response.headers.has('connection', 'keep-alive'))
        else:
            self.assertTrue(response.headers.has('connection', 'close'))
        self.assertTrue('content-type' in response.headers)
        self.assertTrue(response.get_content())
        self.assertRaises(HTTPError, response.raise_for_status)
        
    def test_post(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self.client()
        response = http.post(self.httpbin('post'), encode_multipart=False,
                             data=data)
        yield response.on_finished
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.response, 'OK')
        result = response.content_json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'],['1','2'])
    
    def test_post_multipart(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self.client()
        response = http.post(self.httpbin('post'), data=data)
        yield response.on_finished
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.response, 'OK')
        result = response.content_json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'],['1','2'])
        
    def test_put(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self.client()
        response = http.put(self.httpbin('put'), data=data)
        yield response.on_finished
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.response, 'OK')
        result = response.content_json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'],['1','2'])
        
    def test_patch(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self.client()
        response = http.patch(self.httpbin('patch'), data=data)
        yield response.on_finished
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.response, 'OK')
        result = response.content_json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'],['1','2'])
        
    def test_delete(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self.client()
        response = yield http.delete(self.httpbin('delete'), data=data).on_finished
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.response, 'OK')
        result = response.content_json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'],['1','2'])

    def test_response_headers(self):
        http = self.client()
        response = yield http.get(self.httpbin('response-headers')).on_finished
        self.assertEqual(response.status_code, 200)
        result = response.content_json()
        self.assertEqual(result['Transfer-Encoding'], 'chunked')
        parser = response.parser
        self.assertTrue(parser.is_chunked())
        
    def test_large_response(self):
        http = self.client(timeout=60)
        response = http.get(self.httpbin('getsize/600000'))
        yield response.on_finished
        self.assertEqual(response.status_code, 200)
        data = response.content_json()
        self.assertEqual(data['size'], 600000)
        self.assertEqual(len(data['data']), 600000)
        self.assertFalse(response.parser.is_chunked())
        
    def test_stream_response(self):
        http = self.client()
        response = http.get(self.httpbin('stream/3000/20'))
        yield response.on_finished
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.parser.is_chunked())
        
    def test_expect(self):
        http = self.client()
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        response = yield http.post(self.httpbin('post'), data=data,
                                   wait_continue=True).on_finished
        self.assertEqual(response.status_code, 200)
        
        
class a:
#class CookieAndAuthentication(TestHttpClientBase, unittest.TestCase):
        
    def test_Cookie(self):
        http = self.client()
        # First set the cookies
        r = yield http.get(self.httpbin('cookies', 'set', 'bla', 'foo')).on_finished
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.history)
        self.assertTrue(r.history[0].headers['set-cookie'])
        # Now check if I get them
        r = make_async(http.get(self.httpbin('cookies')))
        yield r
        r = r.result
        self.assertEqual(r.status_code, 200)
        result = r.content_json()
        self.assertTrue(result['cookies'])
        self.assertEqual(result['cookies']['bla'],'foo')
        # Try without saving cookies
        http = self.client(store_cookies=False)
        r = make_async(http.get(self.httpbin('cookies', 'set', 'bla', 'foo')))
        yield r
        r = r.result
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.history)
        self.assertTrue(r.history[0].headers['set-cookie'])
        r = make_async(http.get(self.httpbin('cookies')))
        yield r
        r = r.result
        self.assertEqual(r.status_code, 200)
        result = r.content_json()
        self.assertFalse(result['cookies'])

    def test_parse_cookie(self):
        self.assertEqual(httpurl.parse_cookie('invalid key=true'),
                         {'key':'true'})
        self.assertEqual(httpurl.parse_cookie('invalid;key=true'),
                         {'key':'true'})
        
    def test_basic_authentication(self):
        http = self.client()
        r = make_async(http.get(self.httpbin('basic-auth/bla/foo')))
        yield r
        r = r.result
        self.assertEqual(r.status_code, 401)
        http.add_basic_authentication('bla', 'foo')
        r = make_async(http.get(self.httpbin('basic-auth/bla/foo')))
        yield r
        r = r.result
        self.assertEqual(r.status_code, 200)
        
    def test_digest_authentication(self):
        http = self.client()
        r = make_async(http.get(self.httpbin('digest-auth/auth/bla/foo')))
        yield r
        r = r.result
        self.assertEqual(r.status_code, 401)
        http.add_digest_authentication('bla', 'foo')
        r = make_async(http.get(self.httpbin('digest-auth/auth/bla/foo')))
        yield r
        r = r.result
        self.assertEqual(r.status_code, 200)
    
    #### TO INCLUDE
    def __test_far_expiration(self):
        "Cookie will expire when an distant expiration time is provided"
        response = Response(self.environ())
        response.set_cookie('datetime', expires=datetime(2028, 1, 1, 4, 5, 6))
        datetime_cookie = response.cookies['datetime']
        self.assertEqual(datetime_cookie['expires'], 'Sat, 01-Jan-2028 04:05:06 GMT')

    def __test_max_age_expiration(self):
        "Cookie will expire if max_age is provided"
        response = Response(self.environ())
        response.set_cookie('max_age', max_age=10)
        max_age_cookie = response.cookies['max_age']
        self.assertEqual(max_age_cookie['max-age'], 10)
        self.assertEqual(max_age_cookie['expires'], http.cookie_date(time.time()+10))

    def __test_httponly_cookie(self):
        response = Response(self.environ())
        response.set_cookie('example', httponly=True)
        example_cookie = response.cookies['example']
        # A compat cookie may be in use -- check that it has worked
        # both as an output string, and using the cookie attributes
        self.assertTrue('; httponly' in str(example_cookie))
        self.assertTrue(example_cookie['httponly'])
        
    