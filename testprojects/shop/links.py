"""Names the framework has to resolve, right and wrong side by side.

Every wrong one imports cleanly and passes every test that does not walk that
exact branch.
"""

from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy


def correct_reverse():
    return reverse("confirm-order", args=[1])


def wrong_reverse():
    """NoReverseMatch, the first time somebody takes this branch."""
    return reverse("confirm-ordr", args=[1])


def wrong_reverse_lazy():
    return reverse_lazy("daily-reprot")


def correct_redirect():
    return redirect("daily-report")


def wrong_redirect():
    return redirect("daily-reprot")


def redirect_to_a_path_is_left_alone():
    """A path, not a name. Nothing to look up."""
    return redirect("/orders/1/confirm/")


def redirect_to_an_absolute_url_is_left_alone():
    return redirect("https://example.com/back")


def correct_template(request):
    return render(request, "shop/order_list.html", {})


def wrong_template(request):
    """TemplateDoesNotExist while rendering, not at import."""
    return render(request, "shop/order_lst.html", {})


def a_name_built_at_runtime(request, which):
    """Cannot be resolved without running the code. Deliberately silent."""
    return render(request, f"shop/{which}.html", {})


def send_a_task_that_exists():
    from celery import current_app

    return current_app.send_task("shop.tasks.reconcile")


def send_a_task_that_does_not():
    """The broker accepts the message. The worker rejects it. The sender
    hears nothing either way."""
    from celery import current_app

    return current_app.send_task("shop.tasks.reconsile")
