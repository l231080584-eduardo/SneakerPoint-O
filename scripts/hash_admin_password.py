from getpass import getpass

from werkzeug.security import generate_password_hash


password = getpass("Nueva contraseña administrativa: ")
confirmation = getpass("Confirma la contraseña: ")

if password != confirmation:
    raise SystemExit("Las contraseñas no coinciden.")
if not (
    8 <= len(password) <= 20
    and any(character.isupper() for character in password)
    and any(character.islower() for character in password)
    and any(character.isdigit() for character in password)
    and any(not character.isalnum() and not character.isspace() for character in password)
):
    raise SystemExit("La contraseña debe tener entre 8 y 20 caracteres e incluir mayúscula, minúscula, número y símbolo.")

print(generate_password_hash(password))