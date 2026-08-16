#include <stdio.h>
#include <math.h>

int main() {
    double a, b, c, discriminant, root1, root2;
    scanf("%lf %lf %lf", &a, &b, &c);

    if (a == 0) {
        printf("NOT A QUADRATIC EQUATION\n");
        return 0;
    }

    discriminant = b * b - 4 * a * c;

    if (discriminant > 0) {
        printf("Real and Different Roots\n");
        root1 = (-b + sqrt(discriminant)) / (2 * a);
        root2 = (-b - sqrt(discriminant)) / (2 * a);
        printf("root1 = %.2lf\nroot2 = %.2lf", root1, root2);
    }

    else if (discriminant == 0) {
        printf("Real and Equal Roots\n");
        root1 = root2 = -b / (2 * a);
        printf("root1 = root2 = %.2lf", root1);
    }

    else {
        printf("Imaginary Roots\n");
    }

    return 0;
}
