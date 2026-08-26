
# DocumentCloud Add-On

[Please see DocumentCloud's Add-On documentation](https://github.com/MuckRock/documentcloud-hello-world-addon/wiki/)

# DocumentCloud Forms Text Recovery Add-On

Documentcloud Add-On to recover texts inside PDF form fields.

This Add-On downloads the PDF, reads the values out of the form, and writes
them into the page text along with their word positions.

## Development

    pip install -r requirements.txt

Dependencies are compiled with pip-tools: edit `requirements.in`, re-run `pip-compile`, and commit both files.
