#include <stdio.h>

int main() {
    long long n, mag, reversed_mag, reversed;
    int negative = 0;

    scanf("%lld", &n);

    mag = n;
    if (mag < 0) {
        negative = 1;
        mag = -mag;
    }

    reversed_mag = 0;
    for (long long temp = mag; temp != 0; temp /= 10)
        reversed_mag = reversed_mag * 10 + temp % 10;

    reversed = negative ? -reversed_mag : reversed_mag;

    printf("Reversed = %lld\n", reversed);

    if (!negative && mag == reversed_mag)
        printf("PALINDROME\n");
    else
        printf("NOT PALINDROME\n");

    return 0;
}
