import http.client

connection = http.client.HTTPConnection("service.test")
connection.request("POST", "/payments", body='{"amount_cents": 4200}')
response = connection.getresponse()
