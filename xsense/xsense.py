import requests
import boto3
from botocore.exceptions import ClientError
from pycognito import AWSSRP

from xsense.aws_signer import AWSSigner
from xsense.base import XSenseBase
from xsense.house import House
from xsense.mapping import map_values
from xsense.station import Station


class XSense(XSenseBase):
    def api_call(self, code, unauth=False, **kwargs):
        data = {
            **kwargs
        }

        if unauth:
           headers = None
           mac='abcdefg'
        else:
            headers = {'Authorization': self.access_token}
            mac = self._calculate_mac(data)

        res = requests.post(
            f'{self.API}/app',
            json={
                **data,
                "clientType": self.CLIENTYPE,
                "mac": mac,
                "appVersion": self.VERSION,
                "bizCode": code,
                "appCode": self.APPCODE,
            },
            headers=headers
        )

        self._lastres = res

        data = res.json()
        if data['reCode'] != 200:
            raise RuntimeError(f"Request for code {code} failed with error {data['reCode']} {data.get('reMsg')}")
        return data['reData']

    def get_thing(self, station: Station, page: str):
        headers = {
            'Content-Type': 'application/x-amz-json-1.0',
            'User-Agent': 'aws-sdk-iOS/2.26.5 iOS/17.3 nl_NL',
            'X-Amz-Security-Token': self.session_token
        }

        host = f'{station.house.mqtt_region}.x-sense-iot.com'
        uri = f'/things/SBS50{station.sn}/shadow?name={page}'

        url = f'https://{host}{uri}'

        signed = self.signer.sign_headers('GET', url, station.house.mqtt_region, headers, None)
        headers |= signed
        return requests.get(url, headers=headers).json()

    def login(self, username, password):
        self.username = username
        session = boto3.Session()
        cognito = session.client('cognito-idp', region_name=self.region)

        aws_srp = AWSSRP(
            username=username,
            password=password,
            pool_id=self.userpool,
            client_id=self.clientid,
            client=cognito
        )

        auth_params = aws_srp.get_auth_params()
        auth_params['SECRET_HASH'] = self.generate_hash(username + self.clientid)

        try:
            response = cognito.initiate_auth(
                ClientId=self.clientid,
                AuthFlow='USER_SRP_AUTH',
                AuthParameters=auth_params
            )
        except ClientError as e:
            raise RuntimeError(f'Cannot login, initiate_auth failed: {e}') from e

        userid = response['ChallengeParameters']['USERNAME']

        challenge_response = aws_srp.process_challenge(response["ChallengeParameters"], auth_params)

        challenge_response['SECRET_HASH'] = self.generate_hash(userid + self.clientid)

        try:
            response = cognito.respond_to_auth_challenge(
                ClientId=self.clientid,
                ChallengeName='PASSWORD_VERIFIER',
                ChallengeResponses=challenge_response
            )

            self.access_token = response['AuthenticationResult']['AccessToken']
            self.id_token = response['AuthenticationResult']['IdToken']
            self.refresh_token = response['AuthenticationResult']['RefreshToken']

        except ClientError as e:
            raise RuntimeError(f'Cannot login, respond_to_auth failed: {e}') from e

        self.load_env()

    def init(self):
        self.get_client_info()

    def load_env(self):
        self.get_access_tokens()
        self.signer = AWSSigner(self.access_key, self.secret_access_key, self.session_token)

    def load_all(self):
        result = {}
        for i in self.get_houses():
            h = House(
                i['houseId'],
                i['houseName'],
                i['houseRegion'],
                i['mqttRegion'],
                i['mqttServer']
            )
            result[i['houseId']] = h

            if rooms := self.get_rooms(h.house_id):
                h.set_rooms(rooms)

            if station := self.get_stations(h.house_id):
                h.set_stations(station)
        self.houses = result

    def get_client_info(self):
        data = self.api_call("101001", unauth=True)
        self.clientid = data['clientId']
        self.clientsecret = self._decode_secret(data['clientSecret'])
        self.region = data['cgtRegion']
        self.userpool = data['userPoolId']

    def get_access_tokens(self):
        data = self.api_call("101003", userName=self.username)
        self.access_key = data['accessKeyId']
        self.secret_access_key = data['secretAccessKey']
        self.session_token = data['sessionToken']
        self.token_expiration = data['expiration']

    def get_houses(self):
        params = {
            'utctimestamp': "0"
        }
        return self.api_call("102007", **params)

    def get_rooms(self, houseId: str):
        params = {
            'houseId': houseId,
            'utctimestamp': "0"
        }
        return self.api_call("102008", **params)

    def get_stations(self, houseId: str):
        params = {
            'houseId': houseId,
            'utctimestamp': "0"
        }
        return self.api_call("103007", **params)

    def get_state(self, station: Station):
        res = self.get_thing(station, '2nd_mainpage')

        for sn, i in res['state']['reported']['devs'].items():
            dev = station.get_device_by_sn(sn)
            dev.set_status(i)
