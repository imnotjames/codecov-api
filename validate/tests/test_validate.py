
from rest_framework.test import APITestCase
from rest_framework.reverse import reverse
from rest_framework import status
from unittest.mock import patch
from json import dumps
from yaml import YAMLError

from utils.config import get_config
from covreports.validation.exceptions import InvalidYamlException


class TestValidateYamlHandler(APITestCase):
    
    # Wrap get and post client calls

    def _get(self):
        return self.client.get(reverse('validate-yaml'))

    def _post(self, data=None):
        return self.client.post(reverse('validate-yaml'), data=data, format="json")

    # Unit tests

    def test_get(self):
        response = self._get()

        assert response.status_code == status.HTTP_200_OK

        expected_result = f"Usage:\n\ncurl -X POST --data-binary @codecov.yml {get_config('setup', 'codecov_url')}/validate\n"
        assert response.content.decode() == expected_result

    def test_post_no_data(self):
        response = self._post()
        assert response.status_code == status.HTTP_400_BAD_REQUEST

        expected_result = "No content posted."
        assert response.content.decode() == expected_result

    @patch('validate.views.safe_load')
    def test_post_malformed_yaml(self, mock_safe_load):
        mock_safe_load.side_effect = YAMLError("Can't parse YAML")

        response = self._post(data="malformed yaml")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

        expected_result = "Can't parse YAML\n"
        assert response.content.decode() == expected_result

    @patch('validate.views.validate_yaml')
    def test_post_valid_yaml(self, mock_validate_yaml):
        validated_yaml = {
            "coverage": {
                "round": "down",
                "precision": 2,
                "range": [70.0, 100.0],
                "status": {
                    "project": {
                        "default": {
                            "base": "auto",
                        }
                    }
                }
            },
            "ignore": [
                "Pods/.*",
            ]
        }
        mock_validate_yaml.return_value = validated_yaml
        response = self._post(data="valid yaml")

        assert response.status_code == status.HTTP_200_OK

        expected_result = f"Valid!\n\n{dumps(validated_yaml, indent=2)}\n"
        assert response.content.decode() == expected_result

    @patch('validate.views.validate_yaml')
    def test_post_invalid_yaml(self, mock_validate_yaml):
        mock_validate_yaml.side_effect = InvalidYamlException("Invalid yaml!")

        response = self._post(data="invalid yaml")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        expected_result = "Invalid yaml!\n"
        assert response.content.decode() == expected_result
        