#include <stdio.h>
int main() {
    long long a, b;
    scanf("%lld %lld", &a, &b);

    if (a <= 0 || b <= 0) {
        printf("INVALID INPUT\n");
        return 0;
    }

    long long x = a, y = b, temp;
    while (y != 0) {
        temp = y;
        y = x % y;
        x = temp;
    }
    long long gcd = x;
    long long lcm = (a / gcd) * b;

    printf("GCD = %lld\n", gcd);
    printf("LCM = %lld\n", lcm);

    if (gcd == 1)
        printf("Coprime\n");
    else
        printf("Not Coprime\n");

    return 0;
}
