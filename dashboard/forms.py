import json

from django import forms

from agt.models import AgtConfiguration
from sources.models import DataSource


class PrettyJSONField(forms.JSONField):
    def prepare_value(self, value):
        if isinstance(value, str):
            return value
        return json.dumps(value or {}, indent=2, ensure_ascii=False)


class DataSourceForm(forms.ModelForm):
    mapping = PrettyJSONField(
        label="Mapeamento (JSON)", required=False,
        widget=forms.Textarea(attrs={"rows": 18, "class": "mono", "spellcheck": "false"}),
        help_text="Consultas de leitura e correspondência de colunas. Ver docs/origens.md.",
    )

    class Meta:
        model = DataSource
        fields = ["code", "name", "system", "kind", "active", "connection", "mapping"]

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.instance.company = company
        if self.instance.pk and self.instance.invoices.exists():
            # O código identifica os documentos já importados: não pode mudar.
            for name in ("code", "kind"):
                self.fields[name].disabled = True
                self.fields[name].help_text = "Não pode ser alterado: já há documentos desta origem."

    def clean_code(self):
        code = self.cleaned_data["code"]
        clash = DataSource.objects.filter(company=self.company, code=code).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Já existe uma origem com este código nesta empresa.")
        return code

    def clean_mapping(self):
        return self.cleaned_data.get("mapping") or {}


class AgtConfigurationForm(forms.ModelForm):
    class Meta:
        model = AgtConfiguration
        fields = [
            "environment", "base_url", "software_certificate_number", "software_name", "software_version",
            "client_id", "credentials_prefix", "auto_send", "timeout_seconds", "max_attempts",
            "retry_delay_seconds",
        ]


class ApiKeyForm(forms.Form):
    name = forms.CharField(label="Nome", max_length=100, help_text="Ex.: ERP da loja, POS do restaurante.")
    source = forms.ModelChoiceField(label="Origem", queryset=DataSource.objects.none(),
                                    help_text="Origem do tipo API a que os documentos desta chave pertencem.")

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = DataSource.objects.filter(
            company=company, kind=DataSource.Kind.API, active=True
        )
