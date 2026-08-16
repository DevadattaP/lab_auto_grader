#include<stdio.h>

int main()
{
    int n;
    char ch;
    scanf("%d %c", &n, &ch);

    printf("%s\n", (n & 1) ? "ODD" : "EVEN");

    if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')) {
        ch = ch ^ 32;
        printf("%c", ch);
    } else {
        printf("INVALID CHARACTER");
    }

    return 0;
}
