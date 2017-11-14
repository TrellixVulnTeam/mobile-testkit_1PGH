import json

from requests import Session
from ValueSerializer import ValueSerializer
from Args import Args
from keywords.utils import log_info


class Client:

    def __init__(self, baseUrl):
        self._baseUrl = baseUrl
        self.session = Session()

    def invokeMethod(self, method, args=None, post_data=None):
        try:
            # Create query string from args.
            query = ""

            if args:
                for k, v in args:
                    query += "?" if len(query) == 0 else "&"
                    k_v = "{}={}".format(k, ValueSerializer.serialize(v))
                    query += k_v

            # Create connection to method endpoint.
            url = self._baseUrl + "/" + method + query
            log_info("URL: {}".format(url))
            if post_data:
                headers = {"Content-Type": "application/json"}
                self.session.headers = headers
                resp = self.session.post(url, data=json.dumps(post_data))
            else:
                resp = self.session.post(url)

            resp.raise_for_status()

            # Process response.
            responseCode = resp.status_code
            content_type = None

            try:
                content_type = resp.headers["Content-Type"]
            except:
                pass

            if responseCode == 200:
                result = resp.content
                log_info("Got response: {}".format(result))

                if content_type == "application/json":
                    return ValueSerializer.deserialize(json.loads(result))
                else:
                    return ValueSerializer.deserialize(result)
        except RuntimeError as e:
            raise e
        except Exception as e:
            raise  # RuntimeError(e)

    def release(self, obj):
        args = Args()
        args.setMemoryPointer("object", obj)

        self.invokeMethod("release", args)

    class MethodInvocationException(RuntimeError):
        _responseCode = None
        _responseMessage = None

        def __init__(self, responseCode, responseMessage):
            super(responseMessage)

            self._responseCode = responseCode
            self._responseMessage = responseMessage

        def getResponseCode(self):
            return self._responseCode

        def getResponseMessage(self):
            return self._responseMessage