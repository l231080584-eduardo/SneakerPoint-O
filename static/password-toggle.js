document.querySelectorAll('[data-password-toggle]').forEach((toggle) => {
    const input = toggle.parentElement.querySelector('input[type="password"], input[type="text"]');
    const icon = toggle.querySelector('i');

    if (!input || !icon) return;

    toggle.addEventListener('click', () => {
        const isVisible = input.type === 'text';
        input.type = isVisible ? 'password' : 'text';
        toggle.setAttribute('aria-pressed', String(!isVisible));
        toggle.setAttribute('aria-label', isVisible ? 'Mostrar contraseña' : 'Ocultar contraseña');
        toggle.title = isVisible ? 'Mostrar contraseña' : 'Ocultar contraseña';
        icon.classList.toggle('fa-eye', isVisible);
        icon.classList.toggle('fa-eye-slash', !isVisible);
    });
});
